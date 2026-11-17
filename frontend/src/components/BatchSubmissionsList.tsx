// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import type { KeyboardEvent } from 'react'
import type { SubmissionLockInfo, SubmissionStatus, SubmissionSummary } from '../api/domain'
import {
  assignableSelection,
  bucketCounts,
  canMarkUnresolvable,
  canReviewAnyway,
  closedUnresolvedLabel,
  failureReasonText,
  filterByBucket,
  formatLockAge,
  formatUploadedAt,
  isClosedStatus,
  lockHolderLabel,
  missingPagesLabel,
  showNoCandidate,
  submissionBadgeClass,
  SUBMISSION_BUCKETS,
  truncateSubmissionId,
  unreadLabel,
  type SubmissionBucket,
} from './batchSubmissions'
import { pluralize } from './batchUpload'

interface Props {
  submissions: SubmissionSummary[]
  /** Resolves matched_form_variant_id → variant name. Built ONCE by the page. */
  variantNameById: Map<string, string>
  /** Ticked rows. Owned by the page, which also clears it when the modal closes. */
  selectedIds: ReadonlySet<string>
  onToggleSelected: (submissionId: string) => void
  /** Opens the assignment modal on these submissions — one row's, or the selection. */
  onAssign: (submissionIds: string[]) => void
  /** The signed-in user's id, so their own locks read "You". Undefined while
      the profile loads — then every lock is shown by name. */
  currentUserId: string | undefined
  /** Opens the submission review page of one submission. Called
      only for rows in OPENABLE_STATUSES. */
  onOpen: (submissionId: string) => void
  /** The active filter. Owned by the page, which keeps it in the URL
      (`?filter=`) so it survives opening a submission and coming back. */
  bucket: SubmissionBucket
  onBucketChange: (bucket: SubmissionBucket) => void
  /** "Review anyway" on one submission. The page confirms, runs the
      mutation and navigates; the list only reports the click. */
  onReviewAnyway: (submission: SubmissionSummary) => void
  /** The submission whose "Review anyway" is in flight, if any: its button is
      disabled meanwhile (pessimistic). */
  reviewAnywayPendingId: string | null
  /** "Mark unresolvable" on a MATCH_FAILED or UNASSIGNED row, "Undo
      unresolvable" on an UNRESOLVABLE one. The page confirms (mark only) and runs the
      mutation; the list only reports the click. */
  onMarkUnresolvable: (submission: SubmissionSummary) => void
  onUndoUnresolvable: (submission: SubmissionSummary) => void
  /** The submission whose mark or undo is in flight, if any: its buttons read
      "Marking…" / "Undoing…", and its "Assign variant" is disabled too. */
  unresolvablePendingId: string | null
  /** Whether any mark or undo is in flight: every row's "Mark unresolvable" /
      "Undo unresolvable" is disabled meanwhile, so only one call runs at a
      time (pessimistic). */
  unresolvableBusy: boolean
}

/** The statuses whose submission has a review page worth opening: ready for
    review, or closed (shown read-only). Everything earlier in the pipeline has
    no readings to look at. */
const OPENABLE_STATUSES: readonly SubmissionStatus[] = ['SCANNED', 'REVIEWED', 'FINALIZED']

/**
 * The submissions of a batch, with bucket filters.
 *
 * Presentational on purpose: it queries nothing. The page owns both fetches (the
 * batch and the project's variants) and hands the rows down already resolvable,
 * so the variant column costs zero extra requests no matter how long the list is.
 *
 * Rows of SCANNED, REVIEWED and FINALIZED submissions open the submission review
 * page on click, or on Enter when focused; they alone get the
 * pointer and hover of `nd-row`. Every other row stays inert — there is no
 * reading to look at yet, and a row that highlights but does nothing is worse
 * than a plain one.
 * The other interactive elements are the selection checkbox and "Assign
 * variant", both on MATCH_FAILED rows only — every other status has nothing for
 * an operator to decide, so it gets no checkbox at all rather than a disabled
 * one. They stop propagation, so they never also open a row.
 *
 * Filtering is client-side over the already-fetched array: the endpoint does take
 * a `?status=` filter, but fetching once and bucketing here keeps the pill counts
 * and the rows provably consistent, and makes switching filter instant. Which
 * filter is active is the page's (it lives in the URL).
 *
 * MATCH_FAILED and UNRESOLVABLE rows say why under the badge:
 * red like the badge while the submission waits on a decision, muted once it
 * is closed. A MATCH_FAILED row also offers "Mark unresolvable", per row only
 * (never in the bulk bar), and so does an UNASSIGNED row:
 * the only decision offered on an ungroupable sheet, which otherwise holds
 * back the export for good. An UNRESOLVABLE row offers "Undo unresolvable"
 * and nothing else — it stays inert, as there is no reading to look at.
 *
 * "Review anyway" appears on MATCHED rows a run has examined and
 * left with unread objects — the only action such a row has; the row itself
 * stays inert.
 *
 * "In review by" is read-only: it shows the active review locks the
 * BE reports and never calls a lock endpoint — taking, taking over and
 * releasing a lock belong to the submission page.
 */
export function BatchSubmissionsList({
  submissions, variantNameById, selectedIds, onToggleSelected, onAssign, currentUserId, onOpen,
  bucket, onBucketChange, onReviewAnyway, reviewAnywayPendingId,
  onMarkUnresolvable, onUndoUnresolvable, unresolvablePendingId, unresolvableBusy,
}: Props) {
  const counts = bucketCounts(submissions)
  // NOTE: every submission of the batch is rendered, with no pagination or
  // virtualization — a batch of several hundred may need one of them.
  const rows = filterByBucket(submissions, bucket)

  // Over the WHOLE batch, not the visible rows: a selection made in "To assign"
  // stays live (and stays counted) after switching to another filter.
  const selection = assignableSelection(submissions, selectedIds)
  // The column only exists where something in view can be ticked, so a batch
  // with nothing to assign keeps a table free of a dead 24px gutter.
  const selectable = rows.some(s => s.status === 'MATCH_FAILED')

  return (
    <div className="card mb-16">
      <div className="nd-bucket-filters" role="group" aria-label="Filter submissions by state">
        {SUBMISSION_BUCKETS.map(def => {
          const active = def.key === bucket
          // The buckets that mean work is waiting are coloured — red for a
          // submission that cannot proceed, amber for results awaiting a human,
          // blue for submissions only left to close — but only while
          // they hold something. A coloured pill reading "(0)" would raise an
          // alarm about nothing.
          const attention = def.key === 'TO_ASSIGN' && counts[def.key] > 0
          const review = def.key === 'TO_REVIEW' && counts[def.key] > 0
          const ready = def.key === 'READY_TO_CLOSE' && counts[def.key] > 0
          return (
            <button
              key={def.key}
              type="button"
              className={`nd-bucket${active ? ' is-active' : ''}${attention ? ' is-attention' : ''}${review ? ' is-review' : ''}${ready ? ' is-ready' : ''}`}
              aria-pressed={active}
              onClick={() => onBucketChange(def.key)}
            >
              {def.label} ({counts[def.key]})
            </button>
          )
        })}
      </div>

      {/* Appears only with something ticked: an always-there bar reading "0
          selected" would be furniture, not information. */}
      {selection.length > 0 && (
        <div className="nd-select-bar">
          <span>{pluralize(selection.length, 'submission')} selected</span>
          <button type="button" className="btn btn-primary btn-sm" onClick={() => onAssign(selection)}>
            Assign variant
          </button>
        </div>
      )}

      {rows.length === 0 ? (
        <p className="info-msg">No submissions in this filter.</p>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table className="nd-table">
            <thead>
              <tr>
                {selectable && <th className="nd-select-cell"></th>}
                <th>Submission ID</th>
                <th>Candidate</th>
                <th>Status</th>
                <th>Variant</th>
                <th className="nd-num">Pages</th>
                <th className="nd-num">In review</th>
                <th>Uploaded</th>
                <th>In review by</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {rows.map(submission => (
                <SubmissionRow
                  key={submission.id}
                  submission={submission}
                  variantNameById={variantNameById}
                  selectable={selectable}
                  selected={selectedIds.has(submission.id)}
                  onToggleSelected={onToggleSelected}
                  onAssign={onAssign}
                  currentUserId={currentUserId}
                  onOpen={onOpen}
                  onReviewAnyway={onReviewAnyway}
                  reviewAnywayPending={reviewAnywayPendingId === submission.id}
                  onMarkUnresolvable={onMarkUnresolvable}
                  onUndoUnresolvable={onUndoUnresolvable}
                  unresolvablePending={unresolvablePendingId === submission.id}
                  unresolvableBusy={unresolvableBusy}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function SubmissionRow({
  submission, variantNameById, selectable, selected, onToggleSelected, onAssign, currentUserId, onOpen,
  onReviewAnyway, reviewAnywayPending, onMarkUnresolvable, onUndoUnresolvable, unresolvablePending,
  unresolvableBusy,
}: {
  submission: SubmissionSummary
  variantNameById: Map<string, string>
  selectable: boolean
  selected: boolean
  onToggleSelected: (submissionId: string) => void
  onAssign: (submissionIds: string[]) => void
  currentUserId: string | undefined
  onOpen: (submissionId: string) => void
  onReviewAnyway: (submission: SubmissionSummary) => void
  reviewAnywayPending: boolean
  onMarkUnresolvable: (submission: SubmissionSummary) => void
  onUndoUnresolvable: (submission: SubmissionSummary) => void
  unresolvablePending: boolean
  unresolvableBusy: boolean
}) {
  // Two different reasons for a dash, one display: not matched yet (null id),
  // or matched to a variant the still-loading variants query cannot name yet.
  // Neither is worth blocking the row over — everything else about it is known.
  const variantName = submission.matched_form_variant_id
    ? variantNameById.get(submission.matched_form_variant_id) ?? null
    : null

  const isMatchFailed = submission.status === 'MATCH_FAILED'
  const isUnresolvable = submission.status === 'UNRESOLVABLE'
  const markable = canMarkUnresolvable(submission)
  const reason = failureReasonText(submission)
  const reviewAnyway = canReviewAnyway(submission)
  const missingPages = missingPagesLabel(submission.missing_page_indices)

  const openable = OPENABLE_STATUSES.includes(submission.status)
  const rowProps = openable
    ? {
        className: 'nd-row',
        tabIndex: 0,
        'aria-label': `Open submission ${truncateSubmissionId(submission.id)}`,
        onClick: () => onOpen(submission.id),
        onKeyDown: (e: KeyboardEvent) => {
          // Only the row's own Enter: a key pressed on a control inside it
          // belongs to that control.
          if (e.key === 'Enter' && e.target === e.currentTarget) onOpen(submission.id)
        },
      }
    : {}

  return (
    <tr {...rowProps}>
      {selectable && (
        <td className="nd-select-cell">
          {isMatchFailed && (
            <input
              type="checkbox"
              checked={selected}
              aria-label={`Select submission ${truncateSubmissionId(submission.id)}`}
              onClick={e => e.stopPropagation()}
              onChange={() => onToggleSelected(submission.id)}
            />
          )}
        </td>
      )}
      <td>
        <code title={submission.id}>{truncateSubmissionId(submission.id)}</code>
      </td>
      <td>
        {submission.respondent_identifier ?? <span className="nd-muted">—</span>}
        {showNoCandidate(submission) && <span className="nd-flag">⚠ No candidate</span>}
      </td>
      <td>
        <SubmissionStatusBadge status={submission.status} />
        {reason && (
          <span className={`nd-reason${isUnresolvable ? ' is-closed' : ''}`}>
            {reason.before}
            {reason.code !== null && <code>{reason.code}</code>}
            {reason.after}
          </span>
        )}
        {reviewAnyway && <span className="nd-flag">{unreadLabel(submission.unread_object_count)}</span>}
      </td>
      <td>{variantName ?? <span className="nd-muted">—</span>}</td>
      <td className="nd-num">
        {submission.page_count}
        {missingPages && <span className="nd-flag">⚠ {missingPages}</span>}
      </td>
      <td className="nd-num">
        {inReviewCell(submission)}
      </td>
      <td className="nd-muted">{formatUploadedAt(submission.created_at)}</td>
      <td>
        {submission.lock
          ? <LockCell lock={submission.lock} currentUserId={currentUserId} />
          : <span className="nd-muted">—</span>}
      </td>
      <td style={{ textAlign: 'right' }}>
        {markable && (
          <span className="nd-row-actions">
            {isMatchFailed && (
              /* The single-submission entry point into the same modal the bulk
                 bar opens: it acts on THIS row regardless of what is ticked,
                 so the common case (one failure, fix it now) needs no
                 checkbox. */
              <button
                type="button"
                className="btn btn-secondary btn-sm nd-assign-variant"
                disabled={unresolvablePending}
                onClick={e => {
                  e.stopPropagation()
                  onAssign([submission.id])
                }}
              >
                Assign variant
              </button>
            )}
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              disabled={unresolvableBusy}
              onClick={e => {
                e.stopPropagation()
                onMarkUnresolvable(submission)
              }}
            >
              {unresolvablePending ? 'Marking…' : 'Mark unresolvable'}
            </button>
          </span>
        )}
        {isUnresolvable && (
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            disabled={unresolvableBusy}
            onClick={e => {
              e.stopPropagation()
              onUndoUnresolvable(submission)
            }}
          >
            {unresolvablePending ? 'Undoing…' : 'Undo unresolvable'}
          </button>
        )}
        {reviewAnyway && (
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            disabled={reviewAnywayPending}
            onClick={e => {
              e.stopPropagation()
              onReviewAnyway(submission)
            }}
          >
            Review anyway
          </button>
        )}
      </td>
    </tr>
  )
}

/**
 * The status badge of ONE submission. Local to this list rather than added to the
 * shared StatusBadge: that component's map is the project/variant vocabulary,
 * and the scanning statuses are a different one that happens to reuse the same
 * badge shape.
 */
function SubmissionStatusBadge({ status }: { status: SubmissionStatus }) {
  return <span className={`badge ${submissionBadgeClass(status)}`}>{status}</span>
}

/**
 * "🔒 Mario Rossi · 4 min ago": an ACTIVE review lock (the BE sends null for
 * anything else). The age is computed at render time and is only as fresh as
 * the last refetch — Refresh or window focus — because the queue never polls.
 */
function LockCell({
  lock, currentUserId,
}: {
  lock: SubmissionLockInfo
  currentUserId: string | undefined
}) {
  const mine = currentUserId !== undefined && lock.user_id === currentUserId
  const age = formatLockAge(lock.locked_at)
  return (
    <span className={`nd-lock${mine ? ' is-mine' : ''}`}>
      <span aria-hidden="true">🔒</span>
      {lockHolderLabel(lock, currentUserId)}
      {/* An unparseable timestamp gives no age, and then no dangling "·". */}
      {age && <span className="nd-muted">· {age}</span>}
    </span>
  )
}

/**
 * The In review column: the amber count of readings awaiting a human on an
 * open submission; on a closed one a muted
 * "N unresolved" — what was left at closing, no longer work to do.
 */
function inReviewCell(submission: SubmissionSummary) {
  const n = submission.pending_review_count
  if (isClosedStatus(submission.status)) {
    const label = closedUnresolvedLabel(n)
    return label
      ? <span className="badge badge-unresolved">{label}</span>
      : <span className="nd-muted">—</span>
  }
  return n > 0
    ? <span className="badge badge-review">{n}</span>
    : <span className="nd-muted">—</span>
}
