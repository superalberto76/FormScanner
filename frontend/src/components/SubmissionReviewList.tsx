// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useRef } from 'react'
import type { SubmissionReviewDetail } from '../api/domain'
import {
  objectSubtitle,
  objectTitle,
  outcomeIcon,
  outcomeLabel,
  pageAlignmentWarning,
  pageGroupTitle,
  reasonChipClass,
  reasonLabel,
} from './submissionReview'

interface Props {
  detail: SubmissionReviewDetail
  /** The selected reading's result_id, or null with nothing to review. */
  selectedId: string | null
  onSelect: (resultId: string) => void
  /** "Accept all remaining": offered only when the page passes it —
      lock ours, SCANNED, and something bulk-acceptable left. */
  acceptRemaining?: { count: number; busy: boolean; onClick: () => void }
}

/**
 * The left column of the submission review page: the readings that needed a
 * human, grouped by physical sheet, in the BE's reading order.
 *
 * Presentational: it queries nothing and owns no state — the page holds the
 * selection. Any reading can be selected in any order; nothing here forces a
 * sequence. Resolved readings stay listed (muted, with their outcome's icon —
 * ✓ confirmed, ✎ replaced, ⚑ flagged): they did need a human, and hiding them
 * would make the "done" count unverifiable.
 */
export function SubmissionReviewList({ detail, selectedId, onSelect, acceptRemaining }: Props) {
  const done = detail.review_count - detail.pending_review_count
  // Only sheets with something on them: an unplaced or fully auto-accepted
  // sheet would be a heading over nothing.
  const groups = detail.pages.filter(p => p.objects.length > 0)

  // Whatever moved the selection — a click, ↑ / ↓, or an action moving on to
  // the next reading — its row is brought into view.
  const rows = useRef(new Map<string, HTMLButtonElement>())
  useEffect(() => {
    if (selectedId) rows.current.get(selectedId)?.scrollIntoView?.({ block: 'nearest' })
  }, [selectedId])

  return (
    <div className="card nd-review-list">
      <div className="nd-review-list-head">
        <span>
          {detail.review_count} {detail.review_count === 1 ? 'reading needs' : 'readings need'} review
        </span>
        <span className="nd-review-list-head-side">
          {detail.review_count > 0 && <span>{done} done</span>}
          {acceptRemaining && (
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              disabled={acceptRemaining.busy}
              onClick={acceptRemaining.onClick}
            >
              Accept all remaining ({acceptRemaining.count})
            </button>
          )}
        </span>
      </div>

      {groups.length === 0 ? (
        <p className="nd-review-empty">Nothing to review: every reading was accepted automatically.</p>
      ) : (
        groups.map(page => {
          const alignmentWarning = pageAlignmentWarning(page)
          return (
            <div key={page.id} role="group" aria-label={pageGroupTitle(page)}>
              <div className="nd-review-group-title">{pageGroupTitle(page)}</div>
              {alignmentWarning && (
                <div className="nd-review-group-warning" role="note">{alignmentWarning}</div>
              )}
              {page.objects.map(obj => {
                const selected = obj.result_id === selectedId
                const subtitle = objectSubtitle(obj)
                return (
                  <button
                    key={obj.result_id}
                    ref={el => {
                      if (el) rows.current.set(obj.result_id, el)
                      else rows.current.delete(obj.result_id)
                    }}
                    type="button"
                    // Not a "control" for the page's keys: Enter on a focused
                    // row confirms the reading rather than re-selecting it.
                    data-review-row=""
                    className={`nd-review-obj${selected ? ' is-selected' : ''}${obj.resolved ? ' is-resolved' : ''}`}
                    aria-pressed={selected}
                    onClick={() => onSelect(obj.result_id)}
                  >
                    <span
                      className={`nd-review-check${obj.review_outcome === 'FLAGGED' ? ' is-flagged' : ''}`}
                      aria-label={obj.review_outcome ? outcomeLabel(obj.review_outcome) : undefined}
                    >
                      {obj.review_outcome ? outcomeIcon(obj.review_outcome) : ''}
                    </span>
                    <span className="nd-review-obj-body">
                      <span className="nd-review-obj-title">{objectTitle(obj)}</span>
                      {subtitle && <span className="nd-review-sub" title={subtitle}>{subtitle}</span>}
                      {obj.flag_reasons.length > 0 && <ReasonChips reasons={obj.flag_reasons} />}
                    </span>
                  </button>
                )
              })}
            </div>
          )
        })
      )}
    </div>
  )
}

/** The reason chips of one reading. Shared with the focus panel. */
export function ReasonChips({ reasons }: { reasons: readonly string[] }) {
  return (
    <span className="nd-chips">
      {reasons.map(code => (
        <span key={code} className={`nd-chip ${reasonChipClass(code)}`}>{reasonLabel(code)}</span>
      ))}
    </span>
  )
}
