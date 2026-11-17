// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Pure helpers for the batch's submissions list: the review queue's buckets
// and flags.
// Same split as batchUpload.ts: a plain .ts module (no JSX) so the bucketing,
// the id truncation and the timestamp format can be unit-tested with Node's
// native runner (`node --test`) without rendering React — see
// batchSubmissions.test.ts. Only `import type` from the app runtime: the
// component (BatchSubmissionsList.tsx) holds the markup, the rules live here.
import type {
  MatchFailureReason,
  SubmissionLockInfo,
  SubmissionStatus,
  SubmissionSummary,
} from '../api/domain'

/** The fields the bucket rules read — a subset, so tests need not build a whole row. */
type Bucketable = Pick<SubmissionSummary, 'status' | 'pending_review_count'>

/**
 * The bucket filters of the review queue. Deliberately coarser than
 * SubmissionStatus: an operator triages by "what do I have to do about it", not
 * by the pipeline's internal state.
 *
 * Buckets are predicates, not status lists, because "To review" depends on the
 * pending-review COUNT rather than on a status. A SCANNED submission with 0
 * pending results is "Ready to close": nothing waits
 * on its readings, but a human still has to close it.
 *
 * UNRESOLVABLE has a bucket of its own rather than joining
 * "Reviewed": nobody reviewed it, and its bucket is where it is undone.
 */
export type SubmissionBucket =
  | 'ALL' | 'TO_ASSIGN' | 'TO_EXTRACT' | 'TO_REVIEW' | 'READY_TO_CLOSE' | 'REVIEWED' | 'UNRESOLVABLE'
  | 'UNASSIGNED'

export interface BucketDef {
  key: SubmissionBucket
  label: string
  /** Whether a submission belongs to this bucket. */
  matches: (s: Bucketable) => boolean
}

/** Submissions a human has closed — mirrors the BE's CLOSED_SUBMISSION_STATUSES. */
const CLOSED: readonly SubmissionStatus[] = ['REVIEWED', 'FINALIZED']

/** Whether a human has closed the submission (REVIEWED or FINALIZED). */
export function isClosedStatus(status: SubmissionStatus): boolean {
  return CLOSED.includes(status)
}

/** Bucket order is the display order of the pills. */
export const SUBMISSION_BUCKETS: BucketDef[] = [
  { key: 'ALL', label: 'All', matches: () => true },
  { key: 'TO_ASSIGN', label: 'To assign', matches: s => s.status === 'MATCH_FAILED' },
  {
    key: 'TO_EXTRACT',
    label: 'To extract',
    matches: s => s.status === 'UPLOADED' || s.status === 'MATCHED',
  },
  {
    key: 'TO_REVIEW',
    label: 'To review',
    // A closed submission has no review work left, whatever count it still
    // carries — the same rule the BE applies to the batch-level count.
    matches: s => s.pending_review_count > 0 && !CLOSED.includes(s.status),
  },
  {
    key: 'READY_TO_CLOSE',
    label: 'Ready to close',
    // Open, and every reading that needed a human has been ruled on: what is
    // left is closing it.
    matches: s => s.status === 'SCANNED' && s.pending_review_count === 0,
  },
  { key: 'REVIEWED', label: 'Reviewed', matches: s => CLOSED.includes(s.status) },
  { key: 'UNRESOLVABLE', label: 'Unresolvable', matches: s => s.status === 'UNRESOLVABLE' },
  { key: 'UNASSIGNED', label: 'Unassigned', matches: s => s.status === 'UNASSIGNED' },
]

/** True when `submission` belongs to `bucket` (ALL holds everything). */
export function inBucket(submission: Bucketable, bucket: SubmissionBucket): boolean {
  const def = SUBMISSION_BUCKETS.find(b => b.key === bucket)
  return def ? def.matches(submission) : true
}

/**
 * The rows a bucket shows. Order is preserved verbatim — the BE's order is the
 * only order (no client-side sort, by design).
 */
export function filterByBucket<T extends Bucketable>(
  submissions: readonly T[],
  bucket: SubmissionBucket,
): T[] {
  return submissions.filter(s => inBucket(s, bucket))
}

/**
 * The count shown on every pill, computed once from the SAME fetched array —
 * the filters never hit the server, so a pill can never disagree with the rows.
 */
export function bucketCounts(
  submissions: readonly Bucketable[],
): Record<SubmissionBucket, number> {
  const counts = {} as Record<SubmissionBucket, number>
  for (const def of SUBMISSION_BUCKETS) {
    counts[def.key] = submissions.filter(def.matches).length
  }
  return counts
}

/**
 * The selected submissions that can actually be assigned a variant, in list order.
 *
 * Only MATCH_FAILED rows are selectable, so the intersection is what the
 * contextual bar counts and what the modal acts on: a selected submission that
 * has since been resolved (by another operator, or by an assignment the user
 * just confirmed) must not be counted or re-sent. Order follows the batch's own
 * order, not the order the boxes were ticked in.
 */
export function assignableSelection<T extends { id: string; status: SubmissionStatus }>(
  submissions: readonly T[],
  selectedIds: ReadonlySet<string>,
): string[] {
  return submissions
    .filter(s => s.status === 'MATCH_FAILED' && selectedIds.has(s.id))
    .map(s => s.id)
}

/* Badge classes for a SubmissionStatus. Local to this list on purpose:
   StatusBadge's shared map covers project/variant statuses only, and pouring
   seven scanning statuses into it would make one class carry two meanings.
   Five groups: in progress (blue), ready for review (amber), closed (green),
   failed (red), parked (muted). UNRESOLVABLE is parked, not failed: it is
   closed and nobody has anything left to do about it. */
const BADGE_CLASS_BY_STATUS: Record<SubmissionStatus, string> = {
  UPLOADED: 'badge-sub-processing',
  MATCHED: 'badge-sub-processing',
  SCANNED: 'badge-sub-review',
  REVIEWED: 'badge-sub-done',
  FINALIZED: 'badge-sub-done',
  MATCH_FAILED: 'badge-sub-failed',
  UNASSIGNED: 'badge-sub-unassigned',
  UNRESOLVABLE: 'badge-sub-unassigned',
}

/**
 * The badge class for a status. A status the FE does not know yet (the BE grew
 * one) falls back to the muted style rather than to a coloured one: claiming
 * "done" or "needs attention" about an unknown state would be a lie.
 */
export function submissionBadgeClass(status: SubmissionStatus): string {
  return BADGE_CLASS_BY_STATUS[status] ?? 'badge-sub-unassigned'
}

/**
 * "2 unresolved" — the In review column of a CLOSED submission whose readings
 * were left unresolved at closing, shown muted: it
 * is a fact about the submission, not work to do. Null when there are none.
 */
export function closedUnresolvedLabel(count: number): string | null {
  return count > 0 ? `${count} unresolved` : null
}

// ── Why matching failed ──────────────────────────────────────────────────────

/**
 * The line under a MATCH_FAILED or UNRESOLVABLE badge saying why no variant was
 * resolved, or null for any other status. An UNRESOLVABLE submission marked from
 * UNASSIGNED says "Ungroupable sheet" instead: that is why it
 * was closed, whatever matching may have recorded on it. Split into `before` / `code` /
 * `after` so the component can set the code read in a <code> without this
 * module knowing about markup; `code` is null for the reasons without one.
 *
 * No reason stored means the submission failed before the reason was recorded.
 * The text does not ask for a re-run: the queue has no "Run matching" action.
 */
export interface FailureReasonText {
  before: string
  code: string | null
  after: string
}

export function failureReasonText(
  s: Pick<
    SubmissionSummary,
    'status' | 'match_failure_reason' | 'match_failure_code_value' | 'unresolvable_from_status'
  >,
): FailureReasonText | null {
  if (s.status !== 'MATCH_FAILED' && s.status !== 'UNRESOLVABLE') return null
  const plain = (text: string): FailureReasonText => ({ before: text, code: null, after: '' })
  if (s.status === 'UNRESOLVABLE' && s.unresolvable_from_status === 'UNASSIGNED') {
    return plain('Ungroupable sheet')
  }
  const reason: MatchFailureReason | null = s.match_failure_reason
  if (reason === 'NO_VID_FOUND') return plain('No variant code readable')
  if (reason === 'NO_PUBLISHED_VARIANT') return plain('No published variant in the project')
  if (reason === 'VID_NOT_RECOGNISED') {
    return s.match_failure_code_value !== null
      ? { before: 'Code ', code: s.match_failure_code_value, after: ' is not a published variant' }
      : plain('Code read is not a published variant')
  }
  return plain('Reason not recorded')
}

// ── Mark unresolvable / undo ─────────────────────────────────────────────────

/**
 * Whether a row offers "Mark unresolvable": a MATCH_FAILED submission (no
 * variant anyone can tell) or an UNASSIGNED one (an ungroupable sheet). The
 * BE allows exactly these two; anything else would be a 409 INVALID_STATUS.
 */
export function canMarkUnresolvable(s: Pick<SubmissionSummary, 'status'>): boolean {
  return s.status === 'MATCH_FAILED' || s.status === 'UNASSIGNED'
}

/**
 * What the "Mark as unresolvable" confirmation says follows the submission id.
 * "Closed without a variant" is true of a MATCH_FAILED submission only: an
 * UNASSIGNED one may already carry a variant.
 */
export function markUnresolvableConsequence(status: SubmissionStatus): string {
  const tail = 'it will not be extracted, reviewed or finalized, and it will not hold back the batch export.'
  return status === 'UNASSIGNED'
    ? `will be closed as unresolvable: ${tail}`
    : `will be closed without a variant: ${tail}`
}

/**
 * The toast after "Undo unresolvable", from the status the SERVER returned the
 * submission to — back among the unassigned sheets, or back to assign.
 */
export function unresolvableUndoneToast(status: SubmissionStatus): string {
  return status === 'UNASSIGNED' ? 'Submission back to unassigned' : 'Submission back to assign'
}

/**
 * "2 submissions are ready for extraction." — the hint closing a round of
 * manual assignments (extraction stays on request), or null when
 * no assignment succeeded and nothing is ready.
 */
export function readyForExtractionHint(assigned: number): string | null {
  if (assigned <= 0) return null
  return assigned === 1
    ? '1 submission is ready for extraction.'
    : `${assigned} submissions are ready for extraction.`
}

/**
 * "Missing p. 2, 5" — the Pages column's flag for template pages no sheet was
 * bound to (1-based, as the BE returns them), or null when nothing is missing.
 */
export function missingPagesLabel(indices: readonly number[]): string | null {
  return indices.length > 0 ? `Missing p. ${indices.join(', ')}` : null
}

/**
 * Whether to flag "No candidate": the variant expects a candidate identifier
 * and none was read. A variant without that role has no candidate to miss, so
 * a null identifier there is not a problem worth flagging.
 */
export function showNoCandidate(
  s: Pick<SubmissionSummary, 'respondent_identifier' | 'respondent_identifier_expected'>,
): boolean {
  return s.respondent_identifier_expected && s.respondent_identifier === null
}

/**
 * "a3f1…09e2" — a UUID is unreadable at a glance and useless at full width in a
 * table. The caller carries the full id in a `title`, which is where anyone who
 * actually needs to check one looks.
 */
export function truncateSubmissionId(id: string): string {
  return id.length > 9 ? `${id.slice(0, 4)}…${id.slice(-4)}` : id
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/**
 * "12 Jun 09:14" for this year, "12 Jun 2025 09:14" otherwise. Day-month-name
 * rather than a numeric 12/06, because 12/06 reads as two different
 * dates depending on the reader and the UI is English.
 *
 * `now` is a parameter so the year rule is testable. The BE serialises
 * timezone-aware timestamps, so this renders in the operator's local time.
 */
export function formatUploadedAt(iso: string, now: Date = new Date()): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const day = String(d.getDate()).padStart(2, '0')
  const hours = String(d.getHours()).padStart(2, '0')
  const minutes = String(d.getMinutes()).padStart(2, '0')
  const year = d.getFullYear() === now.getFullYear() ? '' : ` ${d.getFullYear()}`
  return `${day} ${MONTHS[d.getMonth()]}${year} ${hours}:${minutes}`
}

/**
 * Who holds a submission's review lock, as the "In review by" column names them:
 * "You" for the current user (the one case where a name would make the
 * operator double-take), the holder's display name verbatim otherwise.
 * `currentUserId` is undefined while the profile is not loaded, and then
 * nobody is "You".
 */
export function lockHolderLabel(
  lock: Pick<SubmissionLockInfo, 'user_id' | 'display_name'>,
  currentUserId: string | undefined,
): string {
  return currentUserId !== undefined && lock.user_id === currentUserId
    ? 'You'
    : lock.display_name
}

/**
 * "just now", "1 min ago", "12 min ago" — how long ago a lock holder was last
 * active. Hours ("2 h ago") only as a fallback: an active lock is younger than
 * the BE's 15-minute timeout, but a stale cache must not render nonsense.
 *
 * A future timestamp (the operator's clock behind the server's) reads as
 * "just now" rather than as a negative age. An unparseable value gives "",
 * which the caller then simply leaves out.
 *
 * `now` is a parameter so every branch is testable. Recomputed only when the
 * batch is refetched — there is deliberately no ticking timer in the queue.
 */
export function formatLockAge(lockedAtIso: string, now: Date = new Date()): string {
  const lockedAt = new Date(lockedAtIso).getTime()
  if (Number.isNaN(lockedAt)) return ''
  const minutes = Math.floor((now.getTime() - lockedAt) / 60_000)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return minutes === 1 ? '1 min ago' : `${minutes} min ago`
  return `${Math.floor(minutes / 60)} h ago`
}

// ── Review anyway ────────────────────────────────────────────────────────────

/**
 * Whether a row offers "Review anyway": MATCHED, examined by an extraction run
 * at least once, and still holding objects no run could read. The same three
 * conditions the BE checks, so the button is never offered where the action
 * would be refused — in particular never before any run, when every object
 * would become a manual entry.
 */
export function canReviewAnyway(
  s: Pick<SubmissionSummary, 'status' | 'extraction_attempted' | 'unread_object_count'>,
): boolean {
  return s.status === 'MATCHED' && s.extraction_attempted && s.unread_object_count > 0
}

/** "⚠ 1 object unread" / "⚠ 3 objects unread" — the flag under the status. */
export function unreadLabel(n: number): string {
  return `⚠ ${n} ${n === 1 ? 'object' : 'objects'} unread`
}

// ── The active filter in the URL ─────────────────────────────────────────────

/* Slugs rather than the enum keys: the URL is something people copy and read,
   and `?filter=to-review` says what it means. ALL is the absence of a filter. */
const BUCKET_SLUGS: Record<Exclude<SubmissionBucket, 'ALL'>, string> = {
  TO_ASSIGN: 'to-assign',
  TO_EXTRACT: 'to-extract',
  TO_REVIEW: 'to-review',
  READY_TO_CLOSE: 'ready-to-close',
  REVIEWED: 'reviewed',
  UNRESOLVABLE: 'unresolvable',
  UNASSIGNED: 'unassigned',
}

/**
 * The bucket a `?filter=` value names. Missing — or a value the FE does not
 * know (an old bookmark, a typo) — is ALL: an unknown filter must never hide
 * the whole queue behind an empty list.
 */
export function bucketFromParam(value: string | null): SubmissionBucket {
  const found = (Object.keys(BUCKET_SLUGS) as (keyof typeof BUCKET_SLUGS)[])
    .find(key => BUCKET_SLUGS[key] === value)
  return found ?? 'ALL'
}

/** The `?filter=` value for a bucket, or null (no parameter) for ALL. */
export function bucketToParam(bucket: SubmissionBucket): string | null {
  return bucket === 'ALL' ? null : BUCKET_SLUGS[bucket]
}

// ── Next submission ──────────────────────────────────────────────────────────

/**
 * The submission "Next submission →" opens: the first one AFTER `currentId` in
 * the batch's own order — wrapping round to the start — that still has work
 * for this operator:
 *
 *  - open: SCANNED, with results awaiting review or with none left — then the
 *    work is closing it. Which of the two the filter wants is `bucket`'s rule: "To
 *    review" keeps the first, "Ready to close" the second, "All" both;
 *  - not under a colleague's active lock (free, or the operator's own);
 *  - in `bucket`, the queue filter the operator came from;
 *  - not the current one.
 *
 * Null when nothing qualifies. When `currentId` is not in the list (it left
 * the filter, or the list is stale), the search starts from the top.
 * `currentUserId` undefined (profile not loaded) treats every lock as a
 * colleague's: better to skip a submission than to send the operator into one
 * they cannot edit.
 */
export function nextSubmissionId(
  submissions: readonly SubmissionSummary[],
  currentId: string,
  bucket: SubmissionBucket,
  currentUserId: string | undefined,
): string | null {
  const eligible = (s: SubmissionSummary) =>
    s.id !== currentId &&
    s.status === 'SCANNED' &&
    (s.lock === null || (currentUserId !== undefined && s.lock.user_id === currentUserId)) &&
    inBucket(s, bucket)
  const start = submissions.findIndex(s => s.id === currentId) + 1
  const ordered = [...submissions.slice(start), ...submissions.slice(0, start)]
  return ordered.find(eligible)?.id ?? null
}
