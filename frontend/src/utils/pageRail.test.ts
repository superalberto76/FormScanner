// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Unit tests for the pure page-rail helpers. Runs on Node's built-in
// test runner with native TS type-stripping — no extra dev dependency:
//   node --test 'src/**/*.test.ts'   (see package.json "test" script).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  countPageRemovalImpact,
  pageRemovalMessage,
  pageRemovalLinkedChip,
  reorderByDrop,
  sameOrder,
} from './pageRail.ts'

const obj = (shared_across_pages: boolean, shared_group_id: string | null) =>
  ({ shared_across_pages, shared_group_id })

// ── countPageRemovalImpact ───────────────────────────────────────────────────
test('countPageRemovalImpact counts total objects and only linked members', () => {
  const objects = [
    obj(false, null),       // ordinary
    obj(false, 'g1'),       // detached (group id but not shared) — NOT linked
    obj(true, 'g2'),        // linked member — counts
    obj(true, 'g3'),        // linked member — counts
    obj(true, null),        // defensive: shared flag but no group — NOT linked
  ]
  assert.deepEqual(countPageRemovalImpact(objects), { total: 5, linked: 2 })
})

test('countPageRemovalImpact on an empty page is N=0, M=0', () => {
  assert.deepEqual(countPageRemovalImpact([]), { total: 0, linked: 0 })
})

// ── dialog state selection (M === 0 vs M > 0) ────────────────────────────────
test('pageRemovalMessage: M === 0 uses the plain irreversible message', () => {
  const msg = pageRemovalMessage({ total: 3, linked: 0 })
  assert.match(msg, /contains 3 objects/)
  assert.match(msg, /cannot be undone/)
  // No mention of "other pages" in the M === 0 state.
  assert.doesNotMatch(msg, /other pages/)
})

test('pageRemovalMessage: M > 0 explains only this page\'s copies are removed', () => {
  const msg = pageRemovalMessage({ total: 4, linked: 2 })
  assert.match(msg, /contains 4 objects/)
  assert.match(msg, /Only the copies on this page/)
  assert.match(msg, /the copies on the other pages are not touched/)
  assert.match(msg, /cannot be undone/)
})

test('pageRemovalLinkedChip: null when M === 0, text when M > 0', () => {
  assert.equal(pageRemovalLinkedChip(0), null)
  assert.equal(
    pageRemovalLinkedChip(2),
    '🔗 2 objects "shown on all pages" affected',
  )
})

// ── reorderByDrop ────────────────────────────────────────────────────────────
test('reorderByDrop moves an item forward (slot counts original positions)', () => {
  // ['a','b','c','d'] move 'a' to slot 2 → land before original index 2 ('c').
  assert.deepEqual(reorderByDrop(['a', 'b', 'c', 'd'], 'a', 2), ['b', 'a', 'c', 'd'])
})

test('reorderByDrop moves an item backward', () => {
  assert.deepEqual(reorderByDrop(['a', 'b', 'c', 'd'], 'd', 1), ['a', 'd', 'b', 'c'])
})

test('reorderByDrop to slot 0 puts the item first', () => {
  assert.deepEqual(reorderByDrop(['a', 'b', 'c'], 'c', 0), ['c', 'a', 'b'])
})

test('reorderByDrop to slot === length puts the item last', () => {
  assert.deepEqual(reorderByDrop(['a', 'b', 'c'], 'a', 3), ['b', 'c', 'a'])
})

test('reorderByDrop dropping just after itself is a no-op', () => {
  // Moving 'b' (index 1) to slot 2 (right after it) keeps the order.
  assert.deepEqual(reorderByDrop(['a', 'b', 'c'], 'b', 2), ['a', 'b', 'c'])
  // ...and dropping right before itself too.
  assert.deepEqual(reorderByDrop(['a', 'b', 'c'], 'b', 1), ['a', 'b', 'c'])
})

test('reorderByDrop clamps out-of-range slots and ignores an unknown id', () => {
  assert.deepEqual(reorderByDrop(['a', 'b', 'c'], 'a', 99), ['b', 'c', 'a'])
  assert.deepEqual(reorderByDrop(['a', 'b', 'c'], 'zzz', 1), ['a', 'b', 'c'])
})

test('reorderByDrop is a permutation of the same ids (no loss/dupe)', () => {
  const ids = ['p1', 'p2', 'p3', 'p4', 'p5']
  const out = reorderByDrop(ids, 'p4', 0)
  assert.deepEqual([...out].sort(), [...ids].sort())
  assert.equal(out.length, ids.length)
})

// ── sameOrder ────────────────────────────────────────────────────────────────
test('sameOrder detects identical and divergent orders', () => {
  assert.equal(sameOrder(['a', 'b'], ['a', 'b']), true)
  assert.equal(sameOrder(['a', 'b'], ['b', 'a']), false)
  assert.equal(sameOrder(['a'], ['a', 'b']), false)
})
