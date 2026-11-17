// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { batchListStatus, reviewableCount, reviewedPercent } from './batchList.ts'
import type { SubmissionBatchListItem } from '../api/domain.ts'

function item(overrides: Partial<SubmissionBatchListItem> = {}): SubmissionBatchListItem {
  return {
    id: 'b1',
    form_project_id: 'p1',
    created_by_user_id: 'u1',
    name: 'Batch',
    description: null,
    created_at: '2026-09-26T09:40:00+00:00',
    confidence_threshold: null,
    effective_threshold: 0.8,
    total_submissions: 0,
    match_failed_count: 0,
    unassigned_count: 0,
    unresolvable_count: 0,
    reviewed_count: 0,
    pending_review_count: 0,
    ready_for_export: false,
    ...overrides,
  }
}

// ── batchListStatus ──────────────────────────────────────────────────────────
test('an empty batch is empty, with no attention', () => {
  assert.deepEqual(batchListStatus(item()), { kind: 'empty', attention: [], unresolvable: null })
})

test('an empty batch stays empty even if the BE flag said ready', () => {
  assert.equal(batchListStatus(item({ ready_for_export: true })).kind, 'empty')
})

test('ready_for_export makes a batch ready, with no attention', () => {
  assert.deepEqual(
    batchListStatus(item({ total_submissions: 5, reviewed_count: 5, ready_for_export: true })),
    { kind: 'ready', attention: [], unresolvable: null },
  )
})

test('readiness comes from the BE flag, never from the counts', () => {
  // Every submission reviewed, but the BE says not ready: trust the BE.
  const s = batchListStatus(item({ total_submissions: 3, reviewed_count: 3, ready_for_export: false }))
  assert.equal(s.kind, 'in_progress')
})

test('in progress with nothing needing a human has no attention pills', () => {
  assert.deepEqual(
    batchListStatus(item({ total_submissions: 8, reviewed_count: 6 })),
    { kind: 'in_progress', attention: [], unresolvable: null },
  )
})

test('match_failed_count yields a "to assign" pill', () => {
  assert.deepEqual(batchListStatus(item({ total_submissions: 4, match_failed_count: 3 })).attention, [
    { label: '3 to assign', className: 'badge-sub-failed' },
  ])
})

test('unassigned_count yields an "unassigned" pill', () => {
  assert.deepEqual(batchListStatus(item({ total_submissions: 4, unassigned_count: 2 })).attention, [
    { label: '2 unassigned', className: 'badge-sub-unassigned' },
  ])
})

test('both pills together, "to assign" first', () => {
  assert.deepEqual(
    batchListStatus(item({ total_submissions: 42, match_failed_count: 3, unassigned_count: 2 })).attention,
    [
      { label: '3 to assign', className: 'badge-sub-failed' },
      { label: '2 unassigned', className: 'badge-sub-unassigned' },
    ],
  )
})

// ── unresolvable submissions ────────────────────────────────────────────────
const UNRESOLVABLE_PILL = { label: '1 unresolvable', className: 'badge-sub-unassigned' }

test('an unresolvable pill sits next to "Ready for export"', () => {
  assert.deepEqual(
    batchListStatus(item({
      total_submissions: 40, reviewed_count: 39, unresolvable_count: 1, ready_for_export: true,
    })),
    { kind: 'ready', attention: [], unresolvable: UNRESOLVABLE_PILL },
  )
})

test('an unresolvable pill is not attention: nothing to do about it', () => {
  const s = batchListStatus(item({ total_submissions: 22, match_failed_count: 2, unresolvable_count: 1 }))
  assert.equal(s.kind, 'in_progress')
  assert.deepEqual(s.attention, [{ label: '2 to assign', className: 'badge-sub-failed' }])
  assert.deepEqual(s.unresolvable, UNRESOLVABLE_PILL)
})

// Not ready, yet not "In progress" either.
test('a batch of unresolvable submissions only is its own kind', () => {
  assert.deepEqual(
    batchListStatus(item({ total_submissions: 1, unresolvable_count: 1 })),
    { kind: 'unresolvable', attention: [], unresolvable: UNRESOLVABLE_PILL },
  )
})

test('the reviewable count leaves the unresolvable submissions out', () => {
  assert.equal(reviewableCount(item({ total_submissions: 22, unresolvable_count: 1 })), 21)
  assert.equal(reviewableCount(item({ total_submissions: 1, unresolvable_count: 1 })), 0)
})

// ── reviewedPercent ──────────────────────────────────────────────────────────
test('reviewedPercent is null for an empty batch', () => {
  assert.equal(reviewedPercent(item()), null)
})

test('reviewedPercent rounds to an integer', () => {
  assert.equal(reviewedPercent(item({ total_submissions: 42, reviewed_count: 12 })), 29)
  assert.equal(reviewedPercent(item({ total_submissions: 3, reviewed_count: 2 })), 67)
})

test('reviewedPercent spans 0 to 100', () => {
  assert.equal(reviewedPercent(item({ total_submissions: 8, reviewed_count: 0 })), 0)
  assert.equal(reviewedPercent(item({ total_submissions: 5, reviewed_count: 5 })), 100)
})

// A ready batch with one unresolvable submission reaches 100 %.
test('reviewedPercent counts out of the reviewable submissions', () => {
  assert.equal(
    reviewedPercent(item({ total_submissions: 40, reviewed_count: 39, unresolvable_count: 1 })),
    100,
  )
  assert.equal(
    reviewedPercent(item({ total_submissions: 5, reviewed_count: 2, unresolvable_count: 1 })),
    50,
  )
})

test('reviewedPercent of unresolvable submissions only is 0, not null', () => {
  assert.equal(reviewedPercent(item({ total_submissions: 1, unresolvable_count: 1 })), 0)
})

test('reviewedPercent is clamped', () => {
  assert.equal(reviewedPercent(item({ total_submissions: 2, reviewed_count: 5 })), 100)
  assert.equal(reviewedPercent(item({ total_submissions: 2, reviewed_count: -1 })), 0)
})
