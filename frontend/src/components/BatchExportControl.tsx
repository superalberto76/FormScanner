// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { downloadBatchDetail, downloadBatchSummary, type ExportReadiness } from '../api/domain'
import { getErrorCode, normalizeApiError } from '../utils/apiError'
import { useToast } from './ToastProvider'
import {
  EXPORT_CHOICES,
  downloadFileName,
  exportBlockedLine,
  exportChoiceInfo,
  exportFailedText,
  exportKindInfo,
  fileNameError,
  parseExportChoice,
  proposedExportName,
  type ExportChoice,
  type ExportKind,
} from './batchExport'

const DOWNLOADS = { summary: downloadBatchSummary, detail: downloadBatchDetail }

// The format chosen last, on this browser only, shared by the two exports.
// A per-viewer convenience: reading or writing it may throw (private mode, blocked
// storage), and the dialog then simply starts from the default.
const CHOICE_KEY = 'fs_batch_export_format'

function readStoredChoice(): ExportChoice {
  try {
    return parseExportChoice(localStorage.getItem(CHOICE_KEY))
  } catch {
    return parseExportChoice(null)
  }
}

function storeChoice(choice: ExportChoice): void {
  try {
    localStorage.setItem(CHOICE_KEY, choice)
  } catch { /* nothing to remember it in */ }
}

interface Props {
  batchId: string
  batchName: string
  readiness: ExportReadiness
  submissionCount: number
}

/**
 * The "Export" tool of the batch toolbar: "Export summary…" and "Export
 * detail…" open the same dialog, where the format and the file name are
 * chosen. On a batch that is not ready both buttons are disabled and the line
 * under them says why, from the batch detail's `export_readiness`
 * — never recomputed here.
 */
export function BatchExportControl({ batchId, batchName, readiness, submissionCount }: Props) {
  const [open, setOpen] = useState<ExportKind | null>(null)
  const blocked = exportBlockedLine(readiness, submissionCount)

  return (
    <div className="nd-tool">
      <span className="nd-tool-label">Export</span>
      <div className="row row-8">
        {(['summary', 'detail'] as const).map(kind => (
          <button
            key={kind}
            type="button"
            className="btn btn-secondary btn-sm"
            disabled={blocked !== null}
            onClick={() => setOpen(kind)}
          >
            {exportKindInfo(kind).button}
          </button>
        ))}
      </div>
      {blocked && (
        <span className={blocked.tone === 'warning' ? 'nd-export-blocked' : 'nd-run-result'}>
          {blocked.text}
        </span>
      )}
      {open && (
        <ExportModal
          kind={open}
          batchId={batchId}
          batchName={batchName}
          submissionCount={submissionCount}
          onClose={() => setOpen(null)}
        />
      )}
    </div>
  )
}

interface ModalProps {
  kind: ExportKind
  batchId: string
  batchName: string
  submissionCount: number
  onClose: () => void
}

function ExportModal({ kind, batchId, batchName, submissionCount, onClose }: ModalProps) {
  const qc = useQueryClient()
  const toast = useToast()
  const [choice, setChoice] = useState<ExportChoice>(readStoredChoice)
  // Proposed once, when the dialog opens: the time in it is the opening's.
  const [name, setName] = useState(() => proposedExportName(kind, batchName, new Date()))
  const nameError = fileNameError(name)

  const download = useMutation({
    mutationFn: () => {
      const info = exportChoiceInfo(choice)
      return DOWNLOADS[kind](batchId, info.format, info.dialect, downloadFileName(name, choice))
    },
    onSuccess: () => {
      // No toast: the browser already shows the file it saved.
      storeChoice(choice)
      onClose()
    },
    onError: err => {
      if (getErrorCode(err) !== 'NOT_EXPORT_READY') return // shown in the dialog
      // The batch changed after this page loaded (a reopening, an undone
      // unresolvable): say so, and refresh the batch once so the toolbar
      // shows why it is no longer ready.
      toast.error(err)
      void qc.invalidateQueries({ queryKey: ['submission-batch', batchId] })
      onClose()
    },
  })

  const busy = download.isPending
  const failure =
    download.isError && getErrorCode(download.error) !== 'NOT_EXPORT_READY'
      ? exportFailedText(normalizeApiError(download.error))
      : null
  const extension = exportChoiceInfo(choice).extension
  const texts = exportKindInfo(kind)

  return (
    <div className="modal-backdrop" onClick={busy ? undefined : onClose}>
      <div className="modal-box" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h2>{texts.title}</h2>
          {!busy && (
            <button type="button" className="btn btn-secondary btn-sm" onClick={onClose}>
              ✕
            </button>
          )}
        </div>
        <p className="nd-muted mb-16" style={{ fontSize: '0.85rem' }}>
          One row per submission of <b>{batchName}</b> ({submissionCount}){texts.rowsSuffix}
        </p>
        {failure && <p className="error-msg mb-16">{failure}</p>}

        <fieldset className="nd-export-choices" disabled={busy}>
          <legend>Format</legend>
          {EXPORT_CHOICES.map(c => (
            <label className="nd-export-choice" key={c.value}>
              <input
                type="radio"
                name="export-format"
                value={c.value}
                checked={choice === c.value}
                onChange={() => setChoice(c.value)}
              />
              <span>
                {c.label}
                {c.hint && <small>{c.hint}</small>}
              </span>
            </label>
          ))}
        </fieldset>

        <div className="form-group mb-16">
          <label htmlFor="export-file-name">File name</label>
          <div className="nd-export-name">
            <input
              id="export-file-name"
              value={name}
              disabled={busy}
              aria-invalid={nameError !== null}
              onChange={e => setName(e.target.value)}
            />
            <span className="nd-export-ext">.{extension}</span>
          </div>
          {nameError && <span className="nd-field-error">{nameError}</span>}
        </div>

        <div className="row row-8">
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy || nameError !== null}
            onClick={() => download.mutate()}
          >
            {busy ? 'Preparing file…' : 'Download'}
          </button>
          <button type="button" className="btn btn-secondary" disabled={busy} onClick={onClose}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  )
}
