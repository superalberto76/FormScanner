// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import type { SubmissionBatchListItem } from '../api/domain'
import { batchListStatus, reviewableCount, reviewedPercent } from './batchList'
import { formatUploadedAt } from './batchSubmissions'

interface Props {
  /** In the BE's order (newest first), shown verbatim. */
  batches: SubmissionBatchListItem[]
  /** Opens one batch. The page owns the route. */
  onOpen: (batchId: string) => void
}

/**
 * A project's scan batches, one row each, with their metrics.
 *
 * Presentational on purpose: it queries nothing. The page owns the fetch, and
 * every number here comes straight from the BE — the status column included,
 * whose readiness is the BE's `ready_for_export` and never recomputed (see
 * batchList.ts). No client-side sort, filter or pagination: the BE order is
 * the order.
 *
 * Deliberately separate from the Variants table rather than a shared table
 * abstraction: the two lists share a CSS pattern (.nd-table / .nd-row), not
 * columns or behaviour.
 */
export function ProjectBatchesList({ batches, onOpen }: Props) {
  return (
    <div className="card" style={{ padding: 0, overflowX: 'auto' }}>
      <table className="nd-table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Created</th>
            <th className="nd-num">Submissions</th>
            <th>Reviewed</th>
            <th className="nd-num">To review</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {batches.map(batch => {
            const pct = reviewedPercent(batch)
            const status = batchListStatus(batch)
            return (
              <tr key={batch.id} className="nd-row" onClick={() => onOpen(batch.id)}>
                <td>
                  <strong>{batch.name}</strong>
                  {batch.description && (
                    <span className="nd-batch-desc" title={batch.description}>{batch.description}</span>
                  )}
                </td>
                <td className="nd-muted">{formatUploadedAt(batch.created_at)}</td>
                <td className="nd-num">{batch.total_submissions}</td>
                <td>
                  {pct === null ? (
                    <span className="nd-muted">—</span>
                  ) : (
                    <div className="nd-progress">
                      <div
                        className="nd-progress-track"
                        role="progressbar"
                        aria-valuemin={0}
                        aria-valuemax={100}
                        aria-valuenow={pct}
                        aria-label="Reviewed"
                      >
                        <div className="nd-progress-fill" style={{ width: `${pct}%` }} />
                      </div>
                      <span className="nd-progress-label">
                        {batch.reviewed_count} / {reviewableCount(batch)}
                      </span>
                    </div>
                  )}
                </td>
                <td className="nd-num">
                  {batch.pending_review_count > 0 ? (
                    <span className="badge badge-review">{batch.pending_review_count}</span>
                  ) : (
                    <span className="nd-muted">—</span>
                  )}
                </td>
                <td>
                  {status.kind === 'empty' && <span className="nd-muted">No submissions</span>}
                  {status.kind !== 'empty' && (
                    <div className="nd-status-cell">
                      {status.kind === 'ready' && <span className="badge badge-sub-done">Ready for export</span>}
                      {status.kind === 'in_progress' && (
                        <span className="badge badge-sub-processing">In progress</span>
                      )}
                      {status.attention.map(pill => (
                        <span key={pill.label} className={`badge ${pill.className}`}>{pill.label}</span>
                      ))}
                      {status.unresolvable && (
                        <span className={`badge ${status.unresolvable.className}`}>{status.unresolvable.label}</span>
                      )}
                    </div>
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
