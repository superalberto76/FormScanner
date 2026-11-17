// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  assignableSelection,
  bucketCounts,
  bucketFromParam,
  bucketToParam,
  canMarkUnresolvable,
  canReviewAnyway,
  closedUnresolvedLabel,
  failureReasonText,
  markUnresolvableConsequence,
  unresolvableUndoneToast,
  nextSubmissionId,
  readyForExtractionHint,
  unreadLabel,
  filterByBucket,
  formatLockAge,
  formatUploadedAt,
  inBucket,
  isClosedStatus,
  lockHolderLabel,
  missingPagesLabel,
  showNoCandidate,
  submissionBadgeClass,
  SUBMISSION_BUCKETS,
  truncateSubmissionId,
} from './batchSubmissions.ts'
import type { SubmissionStatus, SubmissionSummary } from '../api/domain.ts'

function sub(
  id: string,
  status: SubmissionStatus,
  extra: Partial<SubmissionSummary> = {},
): SubmissionSummary {
  return {
    id,
    status,
    matched_form_variant_id: null,
    match_failure_reason: null,
    match_failure_code_value: null,
    unresolvable_from_status: null,
    page_count: 1,
    created_at: '2026-06-12T09:14:00+00:00',
    pending_review_count: 0,
    respondent_identifier: null,
    missing_page_indices: [],
    respondent_identifier_expected: false,
    lock: null,
    extraction_attempted: false,
    unread_object_count: 0,
    ...extra,
  }
}

/** The buckets a submission lands in, ALL excluded. */
function bucketsOf(s: SubmissionSummary): string[] {
  return SUBMISSION_BUCKETS.filter(b => b.key !== 'ALL' && inBucket(s, b.key)).map(b => b.key)
}

const ALL_STATUSES: SubmissionStatus[] = [
  'UPLOADED', 'UNASSIGNED', 'MATCHED', 'MATCH_FAILED', 'SCANNED', 'REVIEWED', 'FINALIZED',
  'UNRESOLVABLE',
]

// ── buckets ──────────────────────────────────────────────────────────────────
test('the pills are the documented set, in display order', () => {
  assert.deepEqual(
    SUBMISSION_BUCKETS.map(b => b.label),
    ['All', 'To assign', 'To extract', 'To review', 'Ready to close', 'Reviewed', 'Unresolvable', 'Unassigned'],
  )
})

test('ALL holds every status, with or without pending results', () => {
  for (const status of ALL_STATUSES) {
    assert.equal(inBucket(sub('x', status), 'ALL'), true)
    assert.equal(inBucket(sub('x', status, { pending_review_count: 3 }), 'ALL'), true)
  }
})

test('To assign is MATCH_FAILED', () => {
  assert.deepEqual(bucketsOf(sub('x', 'MATCH_FAILED')), ['TO_ASSIGN'])
})

test('To extract is UPLOADED and MATCHED', () => {
  assert.deepEqual(bucketsOf(sub('x', 'UPLOADED')), ['TO_EXTRACT'])
  assert.deepEqual(bucketsOf(sub('x', 'MATCHED')), ['TO_EXTRACT'])
})

test('Unassigned is UNASSIGNED', () => {
  assert.deepEqual(bucketsOf(sub('x', 'UNASSIGNED')), ['UNASSIGNED'])
})

// Not "Reviewed": nobody reviewed it, and not "To assign": it is closed.
test('Unresolvable is UNRESOLVABLE, and only that', () => {
  assert.deepEqual(bucketsOf(sub('x', 'UNRESOLVABLE')), ['UNRESOLVABLE'])
})

test('Reviewed is REVIEWED and FINALIZED', () => {
  assert.deepEqual(bucketsOf(sub('x', 'REVIEWED')), ['REVIEWED'])
  assert.deepEqual(bucketsOf(sub('x', 'FINALIZED')), ['REVIEWED'])
})

test('a SCANNED submission with pending results is To review', () => {
  assert.deepEqual(bucketsOf(sub('x', 'SCANNED', { pending_review_count: 2 })), ['TO_REVIEW'])
})

// Nothing waits on its readings, but nobody has closed it yet.
test('a SCANNED submission with nothing pending is Ready to close', () => {
  assert.deepEqual(bucketsOf(sub('x', 'SCANNED')), ['READY_TO_CLOSE'])
})

test('Ready to close is SCANNED only: not a MATCHED nor a closed submission', () => {
  assert.deepEqual(bucketsOf(sub('x', 'MATCHED')), ['TO_EXTRACT'])
  assert.deepEqual(bucketsOf(sub('x', 'REVIEWED')), ['REVIEWED'])
  assert.deepEqual(bucketsOf(sub('x', 'SCANNED', { pending_review_count: 1 })), ['TO_REVIEW'])
})

test('a closed submission with a leftover pending count is NOT To review', () => {
  for (const status of ['REVIEWED', 'FINALIZED'] as SubmissionStatus[]) {
    assert.deepEqual(bucketsOf(sub('x', status, { pending_review_count: 4 })), ['REVIEWED'])
  }
})

test('To review follows the count, not the status', () => {
  // Pending results on a still-MATCHED submission (a run that errored on one
  // object): it is both still to extract and already reviewable.
  const partial = sub('x', 'MATCHED', { pending_review_count: 1 })
  assert.deepEqual(bucketsOf(partial), ['TO_EXTRACT', 'TO_REVIEW'])
})

test('filterByBucket keeps the backend order and never sorts', () => {
  const list = [sub('c', 'MATCHED'), sub('a', 'UPLOADED'), sub('b', 'MATCHED')]
  assert.deepEqual(filterByBucket(list, 'TO_EXTRACT').map(s => s.id), ['c', 'a', 'b'])
})

test('filterByBucket narrows to the bucket', () => {
  const list = [
    sub('1', 'MATCH_FAILED'),
    sub('2', 'SCANNED', { pending_review_count: 5 }),
    sub('3', 'MATCH_FAILED'),
    sub('4', 'SCANNED'),
  ]
  assert.deepEqual(filterByBucket(list, 'TO_ASSIGN').map(s => s.id), ['1', '3'])
  assert.deepEqual(filterByBucket(list, 'TO_REVIEW').map(s => s.id), ['2'])
  assert.equal(filterByBucket(list, 'UNASSIGNED').length, 0)
  assert.equal(filterByBucket(list, 'ALL').length, 4)
})

test('bucket counts are computed per bucket from the same array', () => {
  const list = [
    sub('1', 'MATCH_FAILED'), sub('2', 'MATCH_FAILED'), sub('3', 'UNASSIGNED'),
    sub('4', 'UPLOADED'), sub('5', 'MATCHED'), sub('6', 'FINALIZED'),
    sub('7', 'SCANNED', { pending_review_count: 3 }), sub('8', 'SCANNED'),
    sub('9', 'UNRESOLVABLE'),
  ]
  const counts = bucketCounts(list)
  assert.equal(counts.ALL, 9)
  assert.equal(counts.TO_ASSIGN, 2)
  assert.equal(counts.TO_EXTRACT, 2)
  assert.equal(counts.TO_REVIEW, 1)
  assert.equal(counts.REVIEWED, 1)
  assert.equal(counts.UNASSIGNED, 1)
  assert.equal(counts.UNRESOLVABLE, 1)
})

test('bucket counts on an empty batch are all zero', () => {
  const counts = bucketCounts([])
  for (const def of SUBMISSION_BUCKETS) assert.equal(counts[def.key], 0)
})

// ── selection ────────────────────────────────────────────────────────────────
test('assignableSelection keeps only the ticked MATCH_FAILED submissions', () => {
  const subs = [
    sub('a', 'MATCH_FAILED'),
    sub('b', 'MATCHED'),
    sub('c', 'MATCH_FAILED'),
  ]
  assert.deepEqual(assignableSelection(subs, new Set(['a', 'b', 'c'])), ['a', 'c'])
})

// A row resolved elsewhere between ticking and confirming must drop out rather
// than be re-sent to an endpoint that would 409 on it.
test('assignableSelection drops a ticked id whose status has moved on', () => {
  const subs = [sub('a', 'MATCHED'), sub('b', 'MATCH_FAILED'), sub('c', 'UNRESOLVABLE')]
  assert.deepEqual(assignableSelection(subs, new Set(['a', 'b', 'c'])), ['b'])
})

test('assignableSelection ignores ids that are not in the batch', () => {
  const subs = [sub('a', 'MATCH_FAILED')]
  assert.deepEqual(assignableSelection(subs, new Set(['zz'])), [])
  assert.deepEqual(assignableSelection(subs, new Set()), [])
})

test('assignableSelection follows the batch order, not the tick order', () => {
  const subs = [sub('a', 'MATCH_FAILED'), sub('b', 'MATCH_FAILED'), sub('c', 'MATCH_FAILED')]
  assert.deepEqual(assignableSelection(subs, new Set(['c', 'a', 'b'])), ['a', 'b', 'c'])
})

// ── badge classes ────────────────────────────────────────────────────────────
test('the badge mapping is the documented one', () => {
  const expected: Record<SubmissionStatus, string> = {
    UPLOADED: 'badge-sub-processing',
    MATCHED: 'badge-sub-processing',
    SCANNED: 'badge-sub-review',
    REVIEWED: 'badge-sub-done',
    FINALIZED: 'badge-sub-done',
    MATCH_FAILED: 'badge-sub-failed',
    UNASSIGNED: 'badge-sub-unassigned',
    UNRESOLVABLE: 'badge-sub-unassigned',
  }
  for (const status of ALL_STATUSES) assert.equal(submissionBadgeClass(status), expected[status])
})

test('MATCH_FAILED is the only status styled as needing attention', () => {
  for (const status of ALL_STATUSES) {
    const isFailed = status === 'MATCH_FAILED'
    assert.equal(submissionBadgeClass(status) === 'badge-sub-failed', isFailed)
  }
})

test('an unknown status falls back to the muted class, never to done/failed', () => {
  const cls = submissionBadgeClass('SOMETHING_NEW' as SubmissionStatus)
  assert.equal(cls, 'badge-sub-unassigned')
})

// ── row flags ────────────────────────────────────────────────────────────────
test('missingPagesLabel lists the indices as returned', () => {
  assert.equal(missingPagesLabel([2, 5]), 'Missing p. 2, 5')
  assert.equal(missingPagesLabel([4]), 'Missing p. 4')
})

test('missingPagesLabel is null when nothing is missing', () => {
  assert.equal(missingPagesLabel([]), null)
})

test('showNoCandidate only when a candidate is expected and none was read', () => {
  const s = (expected: boolean, id: string | null) =>
    sub('x', 'SCANNED', { respondent_identifier_expected: expected, respondent_identifier: id })
  assert.equal(showNoCandidate(s(true, null)), true)
  assert.equal(showNoCandidate(s(true, '0045871')), false)
  assert.equal(showNoCandidate(s(false, null)), false)
  assert.equal(showNoCandidate(s(false, '0045871')), false)
})

test('an empty-string identifier counts as read, not as missing', () => {
  const s = sub('x', 'SCANNED', { respondent_identifier_expected: true, respondent_identifier: '' })
  assert.equal(showNoCandidate(s), false)
})

// ── id truncation ────────────────────────────────────────────────────────────
test('truncateSubmissionId keeps the first and last four chars', () => {
  assert.equal(
    truncateSubmissionId('a3f1c0de-1234-5678-9abc-def012309e2'),
    'a3f1…09e2',
  )
})

test('truncateSubmissionId leaves a short id alone', () => {
  assert.equal(truncateSubmissionId('abc'), 'abc')
  assert.equal(truncateSubmissionId('123456789'), '123456789')
  assert.equal(truncateSubmissionId('1234567890'), '1234…7890')
})

// ── timestamps ───────────────────────────────────────────────────────────────
// No trailing Z: an ISO date-time with no offset is parsed as LOCAL time, so
// these assertions hold whatever the machine's timezone is.
test('formatUploadedAt omits the year within the current year', () => {
  const now = new Date('2026-08-03T00:00:00')
  assert.equal(formatUploadedAt('2026-06-12T09:14:00', now), '12 Jun 09:14')
})

test('formatUploadedAt states the year when it differs', () => {
  const now = new Date('2026-08-03T00:00:00')
  assert.equal(formatUploadedAt('2025-12-01T18:05:00', now), '01 Dec 2025 18:05')
})

test('formatUploadedAt pads day, hours and minutes', () => {
  const now = new Date('2026-08-03T00:00:00')
  assert.equal(formatUploadedAt('2026-01-05T07:09:00', now), '05 Jan 07:09')
})

test('formatUploadedAt returns an unparseable value verbatim', () => {
  assert.equal(formatUploadedAt('not-a-date'), 'not-a-date')
})

// ── review lock ──────────────────────────────────────────────────────────────
const LOCK = { user_id: 'u-mario', display_name: 'Mario Rossi', locked_at: '2026-10-01T09:00:00Z' }

test('lockHolderLabel says "You" for the current user', () => {
  assert.equal(lockHolderLabel(LOCK, 'u-mario'), 'You')
})

test('lockHolderLabel names anyone else by their display name, verbatim', () => {
  assert.equal(lockHolderLabel(LOCK, 'u-luca'), 'Mario Rossi')
  assert.equal(
    lockHolderLabel({ user_id: 'u-x', display_name: '  Unnamed user ' }, 'u-luca'),
    '  Unnamed user ',
  )
})

test('lockHolderLabel names the holder while the current user is unknown', () => {
  assert.equal(lockHolderLabel(LOCK, undefined), 'Mario Rossi')
})

// Explicit Z on both sides: the age is a difference, so the machine's
// timezone cannot matter.
const at = (iso: string) => new Date(iso)

test('formatLockAge says "just now" under a minute', () => {
  assert.equal(formatLockAge('2026-10-01T09:00:00Z', at('2026-10-01T09:00:00Z')), 'just now')
  assert.equal(formatLockAge('2026-10-01T09:00:00Z', at('2026-10-01T09:00:59.999Z')), 'just now')
})

test('formatLockAge says "1 min ago" at one minute', () => {
  assert.equal(formatLockAge('2026-10-01T09:00:00Z', at('2026-10-01T09:01:00Z')), '1 min ago')
  assert.equal(formatLockAge('2026-10-01T09:00:00Z', at('2026-10-01T09:01:59Z')), '1 min ago')
})

test('formatLockAge counts whole minutes under an hour', () => {
  assert.equal(formatLockAge('2026-10-01T09:00:00Z', at('2026-10-01T09:14:30Z')), '14 min ago')
  assert.equal(formatLockAge('2026-10-01T09:00:00Z', at('2026-10-01T09:59:59Z')), '59 min ago')
})

test('formatLockAge falls back to whole hours from an hour on', () => {
  assert.equal(formatLockAge('2026-10-01T09:00:00Z', at('2026-10-01T10:00:00Z')), '1 h ago')
  assert.equal(formatLockAge('2026-10-01T09:00:00Z', at('2026-10-01T11:59:00Z')), '2 h ago')
})

test('formatLockAge reads a future timestamp (clock skew) as "just now"', () => {
  assert.equal(formatLockAge('2026-10-01T09:05:00Z', at('2026-10-01T09:00:00Z')), 'just now')
})

test('formatLockAge returns "" for an unparseable value', () => {
  assert.equal(formatLockAge('not-a-date', at('2026-10-01T09:00:00Z')), '')
})

test('formatLockAge compares instants across offsets', () => {
  // The BE sends UTC, but an offset must not shift the age.
  assert.equal(formatLockAge('2026-10-01T11:00:00+02:00', at('2026-10-01T09:03:00Z')), '3 min ago')
})

// ── Review anyway ────────────────────────────────────────────────────────────

test('review anyway needs MATCHED, an extraction attempt and unread objects', () => {
  const ready = { extraction_attempted: true, unread_object_count: 2 }
  assert.equal(canReviewAnyway(sub('a', 'MATCHED', ready)), true)
  assert.equal(canReviewAnyway(sub('a', 'MATCHED', { ...ready, extraction_attempted: false })), false)
  assert.equal(canReviewAnyway(sub('a', 'MATCHED', { ...ready, unread_object_count: 0 })), false)
  for (const status of ALL_STATUSES.filter(st => st !== 'MATCHED')) {
    assert.equal(canReviewAnyway(sub('a', status, ready)), false, status)
  }
})

test('the unread label is singular for one object', () => {
  assert.equal(unreadLabel(1), '⚠ 1 object unread')
  assert.equal(unreadLabel(3), '⚠ 3 objects unread')
})

// ── Why matching failed ──────────────────────────────────────────────────────

const failed = (extra: Partial<SubmissionSummary>) => sub('x', 'MATCH_FAILED', extra)
const asText = (t: ReturnType<typeof failureReasonText>) =>
  t === null ? null : `${t.before}${t.code ?? ''}${t.after}`

test('each stored reason has its own text', () => {
  assert.equal(
    asText(failureReasonText(failed({ match_failure_reason: 'NO_VID_FOUND' }))),
    'No variant code readable',
  )
  assert.equal(
    asText(failureReasonText(failed({ match_failure_reason: 'NO_PUBLISHED_VARIANT' }))),
    'No published variant in the project',
  )
})

test('an unrecognised code carries the value read, set apart from the text', () => {
  const text = failureReasonText(
    failed({ match_failure_reason: 'VID_NOT_RECOGNISED', match_failure_code_value: 'COLL-MULTI-D' }),
  )
  assert.deepEqual(text, { before: 'Code ', code: 'COLL-MULTI-D', after: ' is not a published variant' })
})

test('an unrecognised code without a value still reads as a sentence', () => {
  const text = failureReasonText(failed({ match_failure_reason: 'VID_NOT_RECOGNISED' }))
  assert.equal(text?.code, null)
  assert.equal(asText(text), 'Code read is not a published variant')
})

test('no reason stored says so, without asking for a re-run', () => {
  assert.equal(asText(failureReasonText(failed({}))), 'Reason not recorded')
})

test('an unresolvable submission keeps showing its reason', () => {
  const s = sub('x', 'UNRESOLVABLE', { match_failure_reason: 'NO_VID_FOUND' })
  assert.equal(asText(failureReasonText(s)), 'No variant code readable')
})

test('an unresolvable submission marked from unassigned is an ungroupable sheet', () => {
  // Matching may have left a reason on it (it spares only the status): the
  // reason it was closed for is the grouping, not the variant.
  const s = sub('x', 'UNRESOLVABLE', {
    unresolvable_from_status: 'UNASSIGNED',
    match_failure_reason: 'NO_VID_FOUND',
  })
  assert.equal(asText(failureReasonText(s)), 'Ungroupable sheet')
})

test('an unresolvable submission marked from match failed keeps its matching reason', () => {
  const s = sub('x', 'UNRESOLVABLE', {
    unresolvable_from_status: 'MATCH_FAILED',
    match_failure_reason: 'NO_PUBLISHED_VARIANT',
  })
  assert.equal(asText(failureReasonText(s)), 'No published variant in the project')
})

test('an unassigned row has no reason line', () => {
  assert.equal(failureReasonText(sub('x', 'UNASSIGNED', { match_failure_reason: 'NO_VID_FOUND' })), null)
})

test('mark unresolvable is offered on match failed and unassigned rows only', () => {
  const offered = ALL_STATUSES.filter(status => canMarkUnresolvable({ status }))
  assert.deepEqual(offered, ['UNASSIGNED', 'MATCH_FAILED'])
})

test('the mark confirmation does not say "without a variant" for an unassigned sheet', () => {
  assert.equal(
    markUnresolvableConsequence('UNASSIGNED'),
    'will be closed as unresolvable: it will not be extracted, reviewed or finalized, and it will not hold back the batch export.',
  )
  assert.equal(
    markUnresolvableConsequence('MATCH_FAILED'),
    'will be closed without a variant: it will not be extracted, reviewed or finalized, and it will not hold back the batch export.',
  )
})

test('the undo toast follows the status the server returned', () => {
  assert.equal(unresolvableUndoneToast('UNASSIGNED'), 'Submission back to unassigned')
  assert.equal(unresolvableUndoneToast('MATCH_FAILED'), 'Submission back to assign')
})

test('no reason line on any other status, even with a stale reason', () => {
  for (const status of ALL_STATUSES) {
    if (status === 'MATCH_FAILED' || status === 'UNRESOLVABLE') continue
    assert.equal(failureReasonText(sub('x', status, { match_failure_reason: 'NO_VID_FOUND' })), null)
  }
})

test('the extraction hint appears only when something was assigned', () => {
  assert.equal(readyForExtractionHint(0), null)
  assert.equal(readyForExtractionHint(1), '1 submission is ready for extraction.')
  assert.equal(readyForExtractionHint(2), '2 submissions are ready for extraction.')
})

// ── Filter ↔ URL ─────────────────────────────────────────────────────────────

test('every bucket round-trips through its URL slug', () => {
  const slugs: [string, string | null][] = [
    ['ALL', null],
    ['TO_ASSIGN', 'to-assign'],
    ['TO_EXTRACT', 'to-extract'],
    ['TO_REVIEW', 'to-review'],
    ['READY_TO_CLOSE', 'ready-to-close'],
    ['REVIEWED', 'reviewed'],
    ['UNRESOLVABLE', 'unresolvable'],
    ['UNASSIGNED', 'unassigned'],
  ]
  for (const [bucket, slug] of slugs) {
    assert.equal(bucketToParam(bucket as never), slug)
    assert.equal(bucketFromParam(slug), bucket)
  }
  assert.equal(SUBMISSION_BUCKETS.length, slugs.length)
})

test('a missing or unknown filter is ALL', () => {
  assert.equal(bucketFromParam(null), 'ALL')
  assert.equal(bucketFromParam(''), 'ALL')
  assert.equal(bucketFromParam('TO_REVIEW'), 'ALL')
  assert.equal(bucketFromParam('nonsense'), 'ALL')
})

// ── Next submission ──────────────────────────────────────────────────────────

const ME = 'me'
const lockBy = (user_id: string) => ({ user_id, display_name: 'X', locked_at: '2026-10-03T09:00:00Z' })
const todo = (id: string, extra: Partial<SubmissionSummary> = {}) =>
  sub(id, 'SCANNED', { pending_review_count: 2, ...extra })

test('next is the first eligible submission after the current one', () => {
  const list = [todo('a'), todo('b'), todo('c')]
  assert.equal(nextSubmissionId(list, 'a', 'ALL', ME), 'b')
  assert.equal(nextSubmissionId(list, 'b', 'ALL', ME), 'c')
})

test('next wraps round to the start of the queue', () => {
  const list = [todo('a'), todo('b'), todo('c')]
  assert.equal(nextSubmissionId(list, 'c', 'ALL', ME), 'a')
})

test('next skips what has no work for this operator', () => {
  const list = [
    todo('current'),
    sub('matched', 'MATCHED', { pending_review_count: 2 }),
    todo('colleague', { lock: lockBy('luca') }),
    sub('closed', 'REVIEWED', { pending_review_count: 2 }),
    todo('mine', { lock: lockBy(ME) }),
  ]
  assert.equal(nextSubmissionId(list, 'current', 'ALL', ME), 'mine')
})

// Closing it is work too.
test('in All, next also stops at an open submission with nothing pending', () => {
  const list = [todo('current'), todo('ready', { pending_review_count: 0 }), todo('pending')]
  assert.equal(nextSubmissionId(list, 'current', 'ALL', ME), 'ready')
  // "To review" still wants something pending.
  assert.equal(nextSubmissionId(list, 'current', 'TO_REVIEW', ME), 'pending')
})

test('next stays inside the active filter', () => {
  // TO_REVIEW holds every SCANNED submission with pending work, so to see the
  // filter bite, use a bucket an eligible submission is NOT in.
  const list = [todo('a'), todo('b')]
  assert.equal(nextSubmissionId(list, 'a', 'TO_REVIEW', ME), 'b')
  assert.equal(nextSubmissionId(list, 'a', 'REVIEWED', ME), null)
})

test('next is null when only the current one qualifies, or nothing does', () => {
  assert.equal(nextSubmissionId([todo('a')], 'a', 'ALL', ME), null)
  assert.equal(nextSubmissionId([], 'a', 'ALL', ME), null)
})

test('a current submission no longer listed starts the search from the top', () => {
  const list = [todo('a'), todo('b')]
  assert.equal(nextSubmissionId(list, 'gone', 'ALL', ME), 'a')
})

test('inside Ready to close, next walks the open submissions with nothing pending', () => {
  const list = [
    todo('current', { pending_review_count: 0 }),
    todo('pending'),
    todo('colleague', { pending_review_count: 0, lock: lockBy('luca') }),
    todo('ready', { pending_review_count: 0 }),
  ]
  assert.equal(nextSubmissionId(list, 'current', 'READY_TO_CLOSE', ME), 'ready')
})

test('with the profile not loaded, any locked submission is skipped', () => {
  const list = [todo('a'), todo('b', { lock: lockBy(ME) }), todo('c')]
  assert.equal(nextSubmissionId(list, 'a', 'ALL', undefined), 'c')
})

// ── Closed submissions ───────────────────────────────────────────────────────

test('isClosedStatus is REVIEWED and FINALIZED only', () => {
  for (const status of ALL_STATUSES) {
    assert.equal(isClosedStatus(status), status === 'REVIEWED' || status === 'FINALIZED', status)
  }
})

test('closedUnresolvedLabel counts what was left, nothing when none', () => {
  assert.equal(closedUnresolvedLabel(2), '2 unresolved')
  assert.equal(closedUnresolvedLabel(1), '1 unresolved')
  assert.equal(closedUnresolvedLabel(0), null)
})
