// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useRef, useState, type DragEvent } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { nd, type SubmissionBatchIngestResult } from '../api/domain'
import { getStatus, normalizeApiError } from '../utils/apiError'
import {
  classifyFiles,
  ingestSummaryLine,
  pluralize,
  selectionWarning,
  SUBMISSION_UPLOAD_ACCEPT,
  totalCount,
  type ClassifiedFiles,
} from './batchUpload'

interface Props {
  batchId: string
}

type PdfRowStatus = 'pending' | 'uploading' | 'done' | 'error' | 'not-attempted'
interface PdfRow {
  file: File
  status: PdfRowStatus
  errorMessage?: string
  /** HTTP status of the failure — a 409 is not retryable, see startUpload(). */
  errorStatus?: number | null
  submissionId?: string
}

type ImageGroupStatus = 'pending' | 'uploading' | 'done' | 'error'
interface ImageGroup {
  files: File[]
  status: ImageGroupStatus
  result?: SubmissionBatchIngestResult
  errorMessage?: string
}

type FlowState = 'empty' | 'selected' | 'uploading' | 'results'

const EMPTY_SELECTION: ClassifiedFiles = { pdfFiles: [], imageFiles: [], ignoredCount: 0 }

/**
 * The batch's ingestion area: ONE dropzone for both backend paths.
 *
 * The user never picks a "mode" — PDFs and loose images are dropped together and
 * split client-side by mime, because which endpoint a file belongs to is a
 * property of the file, not a decision worth asking an operator to make. The two
 * paths then behave differently, and the UI shows that honestly: a PDF is one
 * submission, so PDFs upload one at a time and get one row each; loose images are
 * grouped into submissions ACROSS the request, so they cannot be sent one by one
 * and get a single aggregate row for the single call.
 *
 * Four states — empty (dropzone) → selected → uploading → results — all local:
 * this component only uploads and never reads batch state. The submissions it
 * creates are listed by the batch detail page above it, so a round that
 * created anything invalidates that page's query once, at the end (see
 * invalidateBatch) — the upload's own progress stays in local state regardless.
 */
export function BatchUploadArea({ batchId }: Props) {
  const qc = useQueryClient()
  const [flow, setFlow] = useState<FlowState>('empty')
  const [selection, setSelection] = useState<ClassifiedFiles>(EMPTY_SELECTION)
  const [pdfRows, setPdfRows] = useState<PdfRow[]>([])
  const [imageGroup, setImageGroup] = useState<ImageGroup | null>(null)
  const [dragging, setDragging] = useState(false)
  const [retrying, setRetrying] = useState<number | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  const selecting = flow === 'empty' || flow === 'selected'
  const warning = selecting ? selectionWarning(selection) : null

  function addFiles(files: FileList | File[] | null) {
    if (!files || files.length === 0) return
    const next = classifyFiles(Array.from(files), selection)
    setSelection(next)
    setFlow(totalCount(next) > 0 ? 'selected' : 'empty')
  }

  function removePdf(index: number) {
    const next: ClassifiedFiles = {
      ...selection,
      pdfFiles: selection.pdfFiles.filter((_, i) => i !== index),
    }
    setSelection(next)
    if (totalCount(next) === 0) setFlow('empty')
  }

  function clearImages() {
    const next: ClassifiedFiles = { ...selection, imageFiles: [] }
    setSelection(next)
    if (totalCount(next) === 0) setFlow('empty')
  }

  function onDrop(e: DragEvent<HTMLElement>) {
    e.preventDefault()
    setDragging(false)
    addFiles(e.dataTransfer.files)
  }

  /** Replace one PDF row, so a retry's success drops the previous error fields. */
  function setRow(index: number, next: (row: PdfRow) => PdfRow) {
    setPdfRows(rows => rows.map((r, i) => (i === index ? next(r) : r)))
  }

  /**
   * A call that landed changed the batch's submissions — the list rendered above
   * is now stale. Fired ONCE per upload round rather than per file: a batch of
   * several hundred sheets would otherwise mean several hundred refetches of a
   * list nobody is reading yet, since during 'uploading' the user is watching
   * the per-row progress here, not the table. Never on a round where nothing
   * succeeded — there is no server-side change to pick up.
   *
   * The project's batch lists are stale too (their counts moved), so they are
   * invalidated here as well, by prefix: this component knows the batch, not
   * its project, and the prefix covers whichever project list is cached.
   */
  function invalidateBatch() {
    void qc.invalidateQueries({ queryKey: ['submission-batch', batchId] })
    void qc.invalidateQueries({ queryKey: ['submission-batches'] })
  }

  async function startUpload() {
    const { pdfFiles, imageFiles } = selection
    setPdfRows(pdfFiles.map(file => ({ file, status: 'pending' as const })))
    setImageGroup(imageFiles.length ? { files: imageFiles, status: 'pending' } : null)
    setFlow('uploading')

    // One PDF at a time, awaited: the BE opens a submission per call, and a
    // sequence is what makes the per-row progress mean anything.
    let projectClosed = false
    // Did anything at all land? Decides the single invalidation at the end.
    let created = false
    for (let i = 0; i < pdfFiles.length; i++) {
      if (projectClosed) {
        setRow(i, r => ({ file: r.file, status: 'not-attempted' }))
        continue
      }
      setRow(i, r => ({ file: r.file, status: 'uploading' }))
      try {
        const submission = await nd.uploadSubmissionPdf(batchId, pdfFiles[i])
        setRow(i, r => ({ file: r.file, status: 'done', submissionId: submission.id }))
        created = true
      } catch (err) {
        const status = getStatus(err)
        setRow(i, r => ({
          file: r.file, status: 'error', errorMessage: normalizeApiError(err), errorStatus: status,
        }))
        // Only a 409 stops the queue: it means the project is no longer ACTIVE
        // or no longer ready for scanning (NOT_SCAN_READY), so every
        // remaining call would fail identically and firing them would just be
        // noise. A 413/422 is about THAT file and says nothing about the
        // next one, so the queue carries on.
        if (status === 409) projectClosed = true
      }
    }

    if (imageFiles.length > 0) {
      // Deliberately after the PDF loop rather than alongside it, so the image
      // row's "Queued" is truthful. Still attempted even when a 409 stopped the
      // PDF queue: this is a different call, and if the project really is closed
      // the BE says so on its own row rather than the FE guessing on its behalf.
      setImageGroup(g => (g ? { ...g, status: 'uploading' } : g))
      try {
        const result = await nd.uploadSubmissionImages(batchId, imageFiles)
        setImageGroup(g => (g ? { files: g.files, status: 'done', result } : g))
        created = true
      } catch (err) {
        setImageGroup(g =>
          g ? { files: g.files, status: 'error', errorMessage: normalizeApiError(err) } : g)
      }
    }

    // Both paths are done, so the list below is refreshed exactly once, as the
    // flow lands on 'results' — the moment it starts being worth looking at.
    if (created) invalidateBatch()
    setFlow('results')
  }

  /** Re-send one failed PDF. The File is still in state — nothing to re-pick. */
  async function retryPdf(index: number) {
    const row = pdfRows[index]
    if (!row) return
    setRetrying(index)
    setRow(index, r => ({ file: r.file, status: 'uploading' }))
    try {
      const submission = await nd.uploadSubmissionPdf(batchId, row.file)
      setRow(index, r => ({ file: r.file, status: 'done', submissionId: submission.id }))
      invalidateBatch()
    } catch (err) {
      setRow(index, r => ({
        file: r.file, status: 'error', errorMessage: normalizeApiError(err), errorStatus: getStatus(err),
      }))
    } finally {
      setRetrying(null)
    }
  }

  function reset() {
    setSelection(EMPTY_SELECTION)
    setPdfRows([])
    setImageGroup(null)
    setRetrying(null)
    setFlow('empty')
  }

  const uploadCount = totalCount(selection)

  return (
    <div className="card stack stack-12">
      {selecting && (
        <>
          <button
            type="button"
            className={`nd-dropzone${dragging ? ' is-dragging' : ''}`}
            onClick={() => inputRef.current?.click()}
            onDragOver={e => { e.preventDefault(); setDragging(true) }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
          >
            <span className="nd-dropzone-glyph" aria-hidden="true">↑</span>
            <span className="nd-dropzone-title">Drag PDF or image files here, or choose files</span>
            <span className="nd-dropzone-hint">
              Multipage PDFs or JPG/PNG — you can select several files at once
            </span>
            <span className="nd-dropzone-recommend">
              <strong>Recommended:</strong> one PDF per submission (all the pages of one
              candidate in a single file).
            </span>
          </button>
          <input
            ref={inputRef}
            type="file"
            multiple
            accept={SUBMISSION_UPLOAD_ACCEPT}
            style={{ display: 'none' }}
            onChange={e => {
              addFiles(e.target.files)
              // Reset, so re-picking the very same file fires change again.
              e.target.value = ''
            }}
          />
          {/* The pattern itself is a literal the BE parses: it stays verbatim. */}
          <p className="nd-upload-note">
            Images with no readable code are grouped by filename:{' '}
            <code>&lt;submission&gt;_page_&lt;n&gt;.&lt;ext&gt;</code> — the first token groups the
            pages that belong together and <code>&lt;n&gt;</code> is the page number, starting at 1
            (e.g. <code>smith-john_page_1.jpg</code>). An image matching neither becomes a
            single-page unassigned submission.
          </p>
        </>
      )}

      {flow === 'selected' && (
        <>
          <div className="stack stack-4">
            {selection.pdfFiles.map((file, i) => (
              <div className="nd-upload-row" key={`${file.name}-${file.size}-${file.lastModified}`}>
                <span className="nd-upload-row-name">
                  <span className="nd-upload-kind">PDF</span>
                  {file.name}
                </span>
                <button
                  type="button"
                  className="nd-upload-remove"
                  aria-label={`Remove ${file.name}`}
                  onClick={() => removePdf(i)}
                >
                  ✕
                </button>
              </div>
            ))}
            {selection.imageFiles.length > 0 && (
              <div className="nd-upload-row">
                <span className="nd-upload-row-name">
                  <span className="nd-upload-kind">IMG</span>
                  {pluralize(selection.imageFiles.length, 'image')} selected
                </span>
                <button
                  type="button"
                  className="nd-upload-remove"
                  aria-label="Remove the selected images"
                  onClick={clearImages}
                >
                  ✕
                </button>
              </div>
            )}
          </div>

          {selection.ignoredCount > 0 && (
            <p className="info-msg">
              {pluralize(selection.ignoredCount, 'unsupported file')} ignored — only PDF, JPG and
              PNG are ingested.
            </p>
          )}
          {/* Advisory only: the button stays enabled over the limit. */}
          {warning && <p className="readonly-banner">{warning}</p>}

          <div>
            <button type="button" className="btn btn-primary" onClick={startUpload}>
              Upload {pluralize(uploadCount, 'file')}
            </button>
          </div>
        </>
      )}

      {(flow === 'uploading' || flow === 'results') && (
        <div className="stack stack-4">
          {pdfRows.map((row, i) => (
            <PdfResultRow
              key={`${row.file.name}-${row.file.size}-${row.file.lastModified}`}
              row={row}
              retrying={retrying === i}
              onRetry={flow === 'results' ? () => retryPdf(i) : undefined}
            />
          ))}
          {imageGroup && <ImageResultCard group={imageGroup} />}
        </div>
      )}

      {flow === 'results' && (
        <div>
          <button type="button" className="btn btn-secondary" onClick={reset}>
            Load more files
          </button>
        </div>
      )}
    </div>
  )
}

function PdfResultRow({
  row, retrying, onRetry,
}: { row: PdfRow; retrying: boolean; onRetry?: () => void }) {
  // A 409 stopped the whole queue — retrying it alone would fail the same way
  // until the project is ACTIVE again, so it gets no retry affordance either.
  const canRetry = !!onRetry && row.status === 'error' && row.errorStatus !== 409
  return (
    <div className={`nd-upload-row is-${row.status}`}>
      <span className="nd-upload-row-name">
        <span className="nd-upload-kind">PDF</span>
        {row.file.name}
      </span>
      <span className="row row-8">
        <span className="nd-upload-status">
          {row.status === 'pending' && 'Queued'}
          {row.status === 'uploading' && 'Uploading…'}
          {row.status === 'done' && '✓ Submission created'}
          {row.status === 'error' && `✕ ${row.errorMessage}`}
          {row.status === 'not-attempted' && 'Skipped — the project is no longer active'}
        </span>
        {canRetry && (
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={onRetry}
            disabled={retrying}
          >
            Retry
          </button>
        )}
      </span>
    </div>
  )
}

function ImageResultCard({ group }: { group: ImageGroup }) {
  const label = pluralize(group.files.length, 'image')

  // Before the call runs, the aggregate looks exactly like a PDF row.
  if (group.status === 'pending' || group.status === 'uploading') {
    return (
      <div className={`nd-upload-row is-${group.status}`}>
        <span className="nd-upload-row-name">
          <span className="nd-upload-kind">IMG</span>
          {label}
        </span>
        <span className="nd-upload-status">
          {group.status === 'pending' ? 'Queued' : 'Uploading…'}
        </span>
      </div>
    )
  }

  return (
    <div className={`nd-upload-card is-${group.status}`}>
      <div className="row" style={{ justifyContent: 'space-between' }}>
        <span className="nd-upload-row-name">
          <span className="nd-upload-kind">IMG</span>
          {label}
        </span>
        <span className="nd-upload-status">
          {group.status === 'done' ? '✓ Completed' : '✕ Failed'}
        </span>
      </div>
      {group.status === 'error' && <p className="nd-upload-detail">{group.errorMessage}</p>}
      {group.result && (
        <>
          <p className="nd-upload-detail">{ingestSummaryLine(group.result)}</p>
          {/* Per-file refusals INSIDE a successful call: the rest was ingested,
              and the fix is renaming/rescanning, not re-sending the same bytes —
              so these are reported, not retried. */}
          {group.result.rejected.length > 0 && (
            <ul className="nd-upload-rejected">
              {group.result.rejected.map((r, i) => (
                <li key={`${r.filename}-${i}`}>{r.filename} — {r.reason}</li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  )
}
