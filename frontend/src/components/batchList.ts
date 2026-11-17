// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Pure helpers for the project's batch list.
// Same split as batchSubmissions.ts: a plain .ts module (no JSX) so the status
// derivation and the progress maths can be unit-tested with Node's native
// runner (`node --test`) — see batchList.test.ts. Only `import type` from the
// app runtime: ProjectBatchesList.tsx holds the markup, the rules live here.
import type { SubmissionBatchListItem } from '../api/domain'

export interface AttentionPill {
  label: string
  className: string
}

export interface BatchListStatus {
  /** `unresolvable`: every submission is UNRESOLVABLE — not ready (nothing to
      export), yet nothing is in progress either. */
  kind: 'empty' | 'ready' | 'in_progress' | 'unresolvable'
  /** What still needs a human, in display order. Empty unless in_progress. */
  attention: AttentionPill[]
  /** "N unresolvable": muted, shown next to whatever the status
      is — including "Ready for export" — because it says why a batch has
      submissions nobody reviewed. Null when there are none. */
  unresolvable: AttentionPill | null
}

/**
 * The Status column of a batch row.
 *
 * Readiness is `ready_for_export` from the BE and nothing else: the FE never
 * re-derives it from the counts, so the list can never disagree with the
 * export gate that will enforce it. An empty batch is checked first — the BE
 * already reports it as not ready, but "No submissions" is the honest label,
 * not "In progress". So is a batch holding only unresolvable submissions: the BE
 * reports it not ready, but nothing in it is in progress.
 */
export function batchListStatus(item: SubmissionBatchListItem): BatchListStatus {
  if (item.total_submissions === 0) return { kind: 'empty', attention: [], unresolvable: null }
  const unresolvable = item.unresolvable_count > 0
    ? { label: `${item.unresolvable_count} unresolvable`, className: 'badge-sub-unassigned' }
    : null
  if (item.ready_for_export) return { kind: 'ready', attention: [], unresolvable }
  if (reviewableCount(item) === 0) return { kind: 'unresolvable', attention: [], unresolvable }

  const attention: AttentionPill[] = []
  if (item.match_failed_count > 0) {
    attention.push({ label: `${item.match_failed_count} to assign`, className: 'badge-sub-failed' })
  }
  if (item.unassigned_count > 0) {
    attention.push({ label: `${item.unassigned_count} unassigned`, className: 'badge-sub-unassigned' })
  }
  return { kind: 'in_progress', attention, unresolvable }
}

/**
 * The submissions that can be reviewed: all but the unresolvable ones. The
 * denominator of the "Reviewed" progress, so a batch whose
 * other submissions are all closed reaches 100 %.
 */
export function reviewableCount(item: SubmissionBatchListItem): number {
  return Math.max(0, item.total_submissions - item.unresolvable_count)
}

/**
 * Share of the batch's reviewable submissions a human has closed, as an integer
 * 0–100 for the progress bar. `null` for an empty batch: 0 % of nothing is not
 * progress, and the cell shows "—" instead. A batch of unresolvable submissions
 * only is not empty and shows an empty bar ("0 / 0"). Clamped so a count
 * racing ahead of the total can never draw a bar past its track.
 */
export function reviewedPercent(item: SubmissionBatchListItem): number | null {
  if (item.total_submissions === 0) return null
  const reviewable = reviewableCount(item)
  if (reviewable === 0) return 0
  const pct = Math.round((item.reviewed_count / reviewable) * 100)
  return Math.min(100, Math.max(0, pct))
}
