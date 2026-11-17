// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Unit tests for the pure snap helpers. Runs on Node's built-in test
// runner with native TS type-stripping — no extra dev dependency:
//   node --test 'src/**/*.test.ts'   (see package.json "test" script).
//
// Grid-logic tests pass an explicit `step` so they verify the maths independently
// of the product's configured GRID_STEP default; one test below pins that default.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  GRID_STEP,
  SNAP_THRESHOLD,
  snapToStep,
  snapRectToGrid,
  snapRectToObjects,
  snapPoint,
  snapRect,
  clampRectToArea,
  clipRectToArea,
  areaEdgeGuides,
  roundRect,
  type Rect,
} from './viewerCoords.ts'

const rect = (x: number, y: number, width: number, height: number): Rect => ({ x, y, width, height })

test('GRID_STEP matches the configured lattice (page px)', () => {
  assert.equal(GRID_STEP, 25)
})

test('SNAP_THRESHOLD is a sane positive default', () => {
  assert.ok(SNAP_THRESHOLD > 0)
})

// ── snapToStep ───────────────────────────────────────────────────────────────
test('snapToStep rounds to the nearest multiple of the given step', () => {
  assert.equal(snapToStep(0, 5), 0)
  assert.equal(snapToStep(2, 5), 0) // 2 → nearest 5-multiple is 0
  assert.equal(snapToStep(3, 5), 5) // 3 → 5
  assert.equal(snapToStep(7, 5), 5)
  assert.equal(snapToStep(8, 5), 10)
  assert.equal(snapToStep(12.4, 5), 10)
  assert.equal(snapToStep(13, 10), 10)
  assert.equal(snapToStep(40, 25), 50) // 40 → 50 with a 25 step
})

test('snapToStep defaults to GRID_STEP', () => {
  assert.equal(snapToStep(10), 0) // 10 → nearest 25-multiple is 0
  assert.equal(snapToStep(13), 25) // 13 → 25
})

// ── snapRectToGrid ───────────────────────────────────────────────────────────
test('snapRectToGrid lands both corners on the grid', () => {
  // step 5: left=3→5, top=7→5, right=3+19=22→20, bottom=7+11=18→20.
  const r = snapRectToGrid(rect(3, 7, 19, 11), 5)
  assert.deepEqual(r, { x: 5, y: 5, width: 15, height: 15 })
})

test('snapRectToGrid keeps at least one cell of size', () => {
  // A sub-grid sliver must not collapse below the step.
  const r = snapRectToGrid(rect(1, 1, 1, 1), 25)
  assert.equal(r.width, 25)
  assert.equal(r.height, 25)
})

// ── snapRectToObjects (independent of the grid) ──────────────────────────────
test('snapRectToObjects latches the left edge to a neighbour and preserves size', () => {
  const moving = rect(102, 50, 40, 30)
  const others = [rect(100, 200, 60, 20)] // left edge at x=100
  const { rect: snapped, guides } = snapRectToObjects(moving, others, SNAP_THRESHOLD)
  assert.equal(snapped.x, 100) // shifted left by 2 to align left edges
  assert.equal(snapped.y, 50) // y untouched (no horizontal neighbour line in range)
  assert.equal(snapped.width, 40) // size preserved (rigid translation)
  assert.equal(snapped.height, 30)
  assert.deepEqual(guides, [{ orient: 'v', pos: 100 }])
})

test('snapRectToObjects aligns centres', () => {
  // moving centre x = 200 + 40/2 = 220; neighbour centre x = 218 → shift -2.
  const moving = rect(200, 0, 40, 40)
  const others = [rect(198, 500, 40, 10)] // centre x = 218
  const { rect: snapped } = snapRectToObjects(moving, others, SNAP_THRESHOLD)
  assert.equal(snapped.x + snapped.width / 2, 218)
})

test('snapRectToObjects does not snap beyond the threshold', () => {
  const moving = rect(200, 300, 33, 27) // every edge/centre is >threshold from the neighbour
  const others = [rect(100, 50, 60, 20)]
  const { rect: snapped, guides } = snapRectToObjects(moving, others, SNAP_THRESHOLD)
  assert.deepEqual(snapped, moving)
  assert.deepEqual(guides, [])
})

test('snapRectToObjects picks the nearest of several candidate lines', () => {
  const moving = rect(104, 0, 20, 20) // left=104, right=124
  // neighbour A right edge at 103 (1px from left), neighbour B left at 122 (2px from right)
  const others = [rect(50, 300, 53, 10), rect(122, 400, 30, 10)]
  const { rect: snapped } = snapRectToObjects(moving, others, SNAP_THRESHOLD)
  assert.equal(snapped.x, 103) // 1px pull beats the 2px pull
})

// ── snapPoint (resize corner) ────────────────────────────────────────────────
test('snapPoint latches to an object line, else falls back to grid', () => {
  const others = [rect(100, 100, 50, 50)] // x lines: 100,125,150 ; y lines: 100,125,150
  const { point } = snapPoint({ x: 102, y: 33 }, { grid: true, object: true, others, step: 5 })
  assert.equal(point.x, 100) // object line wins on x
  assert.equal(point.y, 35) // no y line in range → grid fallback (33 → 35 with step 5)
})

test('snapPoint object-snap suppresses grid on the latched axis', () => {
  const others = [rect(101, 0, 10, 10)] // x line at 101 (1px away), within threshold
  const { point, guides } = snapPoint({ x: 102, y: 102 }, { grid: true, object: true, others, step: 5 })
  assert.equal(point.x, 101) // 101, NOT grid-snapped to 100
  assert.equal(point.y, 100) // y had no object line → grid 102 → 100
  assert.deepEqual(guides, [{ orient: 'v', pos: 101 }])
})

test('snapPoint grid-only ignores objects', () => {
  const { point } = snapPoint({ x: 102, y: 33 }, { grid: true, object: false, others: [rect(100, 100, 50, 50)], step: 5 })
  assert.deepEqual(point, { x: 100, y: 35 })
})

// ── snapRect composition ─────────────────────────────────────────────────────
test('snapRect with both behaviours off is the identity', () => {
  const r = rect(3, 7, 19, 11)
  const { rect: out, guides } = snapRect(r, { grid: false, object: false, others: [] })
  assert.deepEqual(out, r)
  assert.deepEqual(guides, [])
})

test('snapRect grid-only on a move preserves size (snapSize false)', () => {
  const { rect: out } = snapRect(rect(3, 7, 19, 11), { grid: true, object: false, others: [], snapSize: false, step: 5 })
  assert.deepEqual(out, { x: 5, y: 5, width: 19, height: 11 }) // only x/y snapped
})

test('snapRect grid-only on a draw snaps size too (snapSize default true)', () => {
  const { rect: out } = snapRect(rect(3, 7, 19, 11), { grid: true, object: false, others: [], step: 5 })
  assert.deepEqual(out, { x: 5, y: 5, width: 15, height: 15 })
})

test('snapRect object-first then grid fallback on the free axis', () => {
  // x latches to neighbour left (100); y has no neighbour → grid fallback.
  const moving = rect(102, 12, 40, 30)
  const others = [rect(100, 400, 40, 10)] // x line 100 (2px); y lines far away
  const { rect: out, guides } = snapRect(moving, { grid: true, object: true, others, snapSize: false, step: 5 })
  assert.equal(out.x, 100) // object-snapped
  assert.equal(out.y, 10) // 12 → grid 10 (object left it free)
  assert.deepEqual(guides, [{ orient: 'v', pos: 100 }])
})

test('snapRect grid does not override an object-snapped axis', () => {
  // Object pulls x to 101 (off-grid). Grid must NOT then re-round x to 100.
  const moving = rect(102, 12, 40, 30)
  const others = [rect(101, 400, 40, 10)] // x line at 101
  const { rect: out } = snapRect(moving, { grid: true, object: true, others, snapSize: false, step: 5 })
  assert.equal(out.x, 101) // preserved off-grid object snap
  assert.equal(out.y, 10) // free axis grid-snapped
})

test('snapRect with snapSize keeps left object-aligned while grid-snapping the far edge', () => {
  // left latches to 101 (object), width grid-snaps via the far edge.
  const moving = rect(102, 0, 21, 40) // right = 123
  const others = [rect(101, 500, 10, 10)]
  const { rect: out } = snapRect(moving, { grid: true, object: true, others, snapSize: true, step: 5 })
  assert.equal(out.x, 101) // object-aligned left (rect translated -1)
  assert.equal(out.x + out.width, 120) // far edge 122 → grid 120
})

// ── Usable area ──────────────────────────────────────────────────────────────
// The A4 usable area: page 794 × 1123 minus the 57 px reserved bands.
const AREA = rect(57, 57, 680, 1009)

test('clampRectToArea leaves a rect already inside the area untouched', () => {
  assert.deepEqual(clampRectToArea(rect(100, 200, 50, 40), AREA), rect(100, 200, 50, 40))
  assert.deepEqual(clampRectToArea(rect(57, 57, 680, 1009), AREA), AREA)
})

test('clampRectToArea stops a move on the edge of the area, size kept', () => {
  assert.deepEqual(clampRectToArea(rect(20, 400, 100, 50), AREA), rect(57, 400, 100, 50)) // left
  assert.deepEqual(clampRectToArea(rect(100, 10, 100, 50), AREA), rect(100, 57, 100, 50)) // top
  assert.deepEqual(clampRectToArea(rect(700, 400, 100, 50), AREA), rect(637, 400, 100, 50)) // right
  assert.deepEqual(clampRectToArea(rect(100, 1100, 100, 50), AREA), rect(100, 1016, 100, 50)) // bottom
})

test('clampRectToArea shrinks a rect larger than the area to its size', () => {
  // A 714 px wide rect becomes 680 wide at x = 57.
  assert.deepEqual(clampRectToArea(rect(40, 900, 714, 80), AREA), rect(57, 900, 680, 80))
})

test('clipRectToArea cuts only the edges past the area (anchored corner stays)', () => {
  // Resize from the NW anchor (600, 300) to a pointer in the right band.
  assert.deepEqual(clipRectToArea(rect(600, 300, 180, 60), AREA), rect(600, 300, 137, 60))
  // An object in the left band, resized from its SE corner: left edge → 57.
  assert.deepEqual(clipRectToArea(rect(20, 300, 200, 60), AREA), rect(57, 300, 163, 60))
})

test('clipRectToArea keeps the minimum size inside the area', () => {
  // A box drawn wholly in the left band ends as a MIN-size box on the edge.
  const r = clipRectToArea(rect(5, 300, 30, 30), AREA)
  assert.equal(r.x, 57)
  assert.ok(r.width >= 8 && r.height >= 8)
  assert.ok(r.x + r.width <= 737)
})

test('areaEdgeGuides names the edges the asked rect crosses', () => {
  assert.deepEqual(areaEdgeGuides(rect(100, 200, 50, 40), AREA), [])
  assert.deepEqual(areaEdgeGuides(rect(20, 200, 50, 40), AREA), [{ orient: 'v', pos: 57 }])
  assert.deepEqual(areaEdgeGuides(rect(700, 1050, 50, 40), AREA), [
    { orient: 'v', pos: 737 },
    { orient: 'h', pos: 1066 },
  ])
})

test('rounding a clipped rect then clamping it again stays inside the area', () => {
  // 600.5 + 136.5 = 737 exactly; rounding both up would give a right edge of 738.
  const rounded = roundRect(clipRectToArea(rect(600.5, 300, 200, 60), AREA))
  assert.equal(rounded.x + rounded.width, 738)
  const r = clampRectToArea(rounded, AREA)
  assert.ok(r.x + r.width <= 737)
  assert.equal(r.width, 137)
})
