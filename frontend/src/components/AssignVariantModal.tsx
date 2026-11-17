// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState } from 'react'
import { createPortal } from 'react-dom'
import { useQueryClient } from '@tanstack/react-query'
import { nd, type FormVariantListItem } from '../api/domain'
import { normalizeApiError } from '../utils/apiError'
import { readyForExtractionHint, truncateSubmissionId } from './batchSubmissions'
import { pluralize } from './batchUpload'

interface Props {
  /** The submissions to assign: one for the per-row button, N for the bulk bar. */
  submissionIds: string[]
  /** Already filtered to PUBLISHED by the page — this modal fetches nothing. */
  publishedVariants: FormVariantListItem[]
  /** Called on Cancel AND on Close-after-results; the caller drops the selection. */
  onClose: () => void
  batchId: string
}

type RowStatus = 'pending' | 'in-progress' | 'done' | 'error'
interface AssignRow {
  submissionId: string
  status: RowStatus
  errorMessage?: string
}

/** picking → running → results. There is no way back to picking: see close(). */
type Phase = 'picking' | 'running' | 'results'

/**
 * Manual variant assignment for MATCH_FAILED submissions, single or in bulk.
 *
 * ONE component for both entry points: a per-row button hands it one id, the
 * bulk bar hands it several, and nothing else about the flow differs — so there
 * is one modal, not two near-identical ones.
 *
 * The queue is sequential and mirrors the PDF upload loop: one call
 * per submission, awaited, with per-item state so the progress means something.
 * UNLIKE that loop it has no stop condition at all — every failure this endpoint
 * can produce (409 not MATCH_FAILED, 409 variant not published, 404 unknown
 * variant) is about ONE submission and says nothing about the next, so a failure
 * is recorded on its row and the queue carries on. There is no abort control
 * either: once confirmed, the round runs to the end of the selection.
 *
 * Portal to <body>, backdrop/box markup and classes copied from
 * ObjectCreateModal / VariantEditModal — the established shape in this codebase.
 */
export function AssignVariantModal({ submissionIds, publishedVariants, onClose, batchId }: Props) {
  const qc = useQueryClient()
  const [variantId, setVariantId] = useState('')
  const [phase, setPhase] = useState<Phase>('picking')
  const [rows, setRows] = useState<AssignRow[]>([])

  /** Replace one row wholesale, so a status change drops the previous error. */
  function setRow(index: number, next: (row: AssignRow) => AssignRow) {
    setRows(rs => rs.map((r, i) => (i === index ? next(r) : r)))
  }

  async function confirm() {
    setRows(submissionIds.map(id => ({ submissionId: id, status: 'pending' as const })))
    setPhase('running')

    for (let i = 0; i < submissionIds.length; i++) {
      setRow(i, r => ({ submissionId: r.submissionId, status: 'in-progress' }))
      try {
        await nd.assignSubmissionVariant(submissionIds[i], variantId)
        setRow(i, r => ({ submissionId: r.submissionId, status: 'done' }))
      } catch (err) {
        setRow(i, r => ({
          submissionId: r.submissionId, status: 'error', errorMessage: normalizeApiError(err),
        }))
      }
    }

    setPhase('results')
  }

  /**
   * Every way out of the modal (Cancel, Close, ✕, backdrop) lands here.
   *
   * A round that ran invalidates the batch ONCE, on the way out — not per item:
   * during the queue the user is watching these rows, not the table behind, and
   * a bulk of several hundred would otherwise refetch the list as many times.
   * Unconditional after a round, even if every call failed: "already resolved by
   * another operator" is itself proof the list on screen is stale. Cancelling
   * from the picker fired nothing, so there is nothing to pick up.
   *
   * The project's batch lists count MATCH_FAILED submissions too, so they are
   * invalidated alongside, by prefix (this modal knows the batch, not its
   * project).
   */
  function close() {
    if (phase === 'results') {
      void qc.invalidateQueries({ queryKey: ['submission-batch', batchId] })
      void qc.invalidateQueries({ queryKey: ['submission-batches'] })
    }
    onClose()
  }

  // Closing mid-queue would throw away the results of calls that already landed
  // while the remaining ones kept running — so the exits are gone until it ends.
  const closable = phase !== 'running'
  const noVariants = publishedVariants.length === 0
  // Extraction stays the operator's call: the round only
  // points to it, and only when something actually became ready.
  const hint = phase === 'results'
    ? readyForExtractionHint(rows.filter(r => r.status === 'done').length)
    : null
  const title = phase === 'picking'
    ? 'Assign variant'
    : phase === 'running' ? 'Assigning variant…' : 'Assign variant — results'

  return createPortal(
    <div className="modal-backdrop" onClick={closable ? close : undefined}>
      <div className="modal-box" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h2>{title}</h2>
          {closable && <button className="btn btn-secondary btn-sm" onClick={close}>✕</button>}
        </div>

        {phase === 'picking' ? (
          <>
            <p className="nd-muted mb-16">{pluralize(submissionIds.length, 'submission')} selected</p>

            {/* One chip per target, truncated like the list's id column; the full
                id stays in the title for anyone who needs to check one. */}
            <div className="nd-assign-chips">
              {submissionIds.map(id => (
                <span className="nd-assign-chip" key={id} title={id}>{truncateSubmissionId(id)}</span>
              ))}
            </div>

            <div className="form-group">
              <label htmlFor="assign-variant-select">Variant</label>
              <select
                id="assign-variant-select"
                value={variantId}
                disabled={noVariants}
                onChange={e => setVariantId(e.target.value)}
              >
                {/* No pre-selected variant: confirming binds every submission in
                    the selection at once, so the choice must be deliberate. */}
                <option value="">Select a published variant…</option>
                {publishedVariants.map(v => (
                  <option key={v.id} value={v.id}>{v.name} — v{v.version_number}</option>
                ))}
              </select>
            </div>

            {noVariants && (
              <p className="info-msg mb-16">
                This project has no published variant yet — publish one before assigning.
              </p>
            )}

            <div className="row row-8">
              <button type="button" className="btn btn-primary" disabled={!variantId} onClick={confirm}>
                Confirm
              </button>
              <button type="button" className="btn btn-secondary" onClick={close}>Cancel</button>
            </div>
          </>
        ) : (
          <>
            <div className="stack stack-4 mb-16">
              {rows.map(row => <AssignResultRow key={row.submissionId} row={row} />)}
            </div>
            {hint && (
              <p className="nd-assign-hint mb-16">
                {hint} Use <strong>Run extraction</strong> on this page when you are ready.
              </p>
            )}
            {phase === 'results' && (
              <div>
                <button type="button" className="btn btn-primary" onClick={close}>Close</button>
              </div>
            )}
          </>
        )}
      </div>
    </div>,
    document.body,
  )
}

/** One submission's outcome — same shape and wording as the upload area's rows. */
function AssignResultRow({ row }: { row: AssignRow }) {
  return (
    <div className={`nd-assign-row is-${row.status}`}>
      <code title={row.submissionId}>{truncateSubmissionId(row.submissionId)}</code>
      <span className="nd-assign-status">
        {row.status === 'pending' && 'Queued'}
        {row.status === 'in-progress' && 'Assigning…'}
        {row.status === 'done' && '✓ Assigned'}
        {row.status === 'error' && `✕ ${row.errorMessage}`}
      </span>
    </div>
  )
}
