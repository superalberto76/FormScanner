// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// FREEZE TEST — TS side. Asserts the canvas constants in
// exportLayout.ts equal the values frozen from the PDF-export modules. The Python
// twin (backend/tests/test_export_layout_freeze.py) freezes the SAME numbers from
// the real export code. If the two drift, exactly one side's freeze test fails —
// catching silent divergence.
//
// Runs on Node's built-in runner with native TS type-stripping:
//   node --test 'src/**/*.test.ts'   (see package.json "test").
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  AUTOFIT,
  CODE,
  COLORS,
  cssFontStack,
  FT,
  holeRect,
  IMAGE_PLACEHOLDER,
  inReservedBand,
  M,
  markRects,
  MARKS,
  SIDE_MARKS,
  sideMarkRects,
  Q,
  usableRect,
} from './exportLayout.ts'

test('COLORS mirror the export 0..1 RGB tuples (0.45→115, 0.72→184)', () => {
  assert.equal(COLORS.BLACK, '#000')
  assert.equal(COLORS.GRAY, 'rgb(115,115,115)')
  assert.equal(COLORS.LIGHT, 'rgb(184,184,184)')
  assert.equal(COLORS.WHITE, '#fff')
})

test('cssFontStack replicates fitz_font substring detection', () => {
  // Courier bucket: "cour" / "mono"
  assert.equal(cssFontStack('Courier New'), '"Courier New", monospace')
  assert.equal(cssFontStack('Roboto Mono'), '"Courier New", monospace')
  // Times bucket: "times" / "serif"
  assert.equal(cssFontStack('Times New Roman'), '"Times New Roman", serif')
  assert.equal(cssFontStack('Noto Serif'), '"Times New Roman", serif')
  // Default Helvetica bucket (incl. null/empty)
  assert.equal(cssFontStack('Helvetica'), 'Arial, "Liberation Sans", sans-serif')
  assert.equal(cssFontStack('Arial'), 'Arial, "Liberation Sans", sans-serif')
  assert.equal(cssFontStack(null), 'Arial, "Liberation Sans", sans-serif')
  assert.equal(cssFontStack(undefined), 'Arial, "Liberation Sans", sans-serif')
})

test('AUTOFIT (PageRenderer.text) frozen', () => {
  assert.equal(AUTOFIT.SHRINK, 0.9)
  assert.equal(AUTOFIT.FLOOR_PX, 1)
})

test('QUESTION constants frozen (_Q_* + header/option sizes)', () => {
  assert.deepEqual(
    { ...Q },
    { HEADER_H: 18, OPTION_H: 18, CIRCLE_R: 5, CIRCLE_CX: 9, TEXT_X: 22, HEADER_SIZE: 12, OPTION_SIZE: 11 },
  )
})

test('MATRIX constants frozen (_M_* + label_w + sizes; CIRCLE_R == _Q_CIRCLE_R)', () => {
  assert.deepEqual(
    { ...M },
    { LABEL_H: 16, HEADER_H: 16, LABEL_W_RATIO: 0.25, LABEL_W_MAX: 120, CIRCLE_R: 5, LABEL_SIZE: 12, HEADER_SIZE: 9, ROW_LABEL_SIZE: 9 },
  )
  assert.equal(M.CIRCLE_R, Q.CIRCLE_R)
})

test('FREE_TEXT_AREA constants frozen (_FT_LABEL_H + sizes/padding)', () => {
  assert.deepEqual(
    { ...FT },
    { LABEL_H: 14, LABEL_SIZE: 10, PLACEHOLDER_SIZE: 9, PAD_X: 4, PAD_Y: 3, PLACEHOLDER_MAX_H: 16 },
  )
})

test('CODE constants frozen (_HUMAN_READABLE_STRIP_PX + text size/pad)', () => {
  assert.deepEqual({ ...CODE }, { HUMAN_READABLE_STRIP_PX: 14, TEXT_SIZE: 8, PAD: 3 })
})

test('IMAGE placeholder label size frozen', () => {
  assert.equal(IMAGE_PLACEHOLDER.LABEL_SIZE, 9)
})

test('MARKS constants frozen (alignment_marks: BAND_W / MARK_INSET / MARK_SIZE / HOLE_SIZE)', () => {
  assert.deepEqual(
    { ...MARKS },
    { BAND_W: 57, INSET: 30, SIZE: 19, HOLE_SIZE: 7, DISTINCT_CORNER: 'BR' },
  )
})

test('markRects on the default A4 page matches the backend geometry', () => {
  const r = markRects(794, 1123)
  assert.deepEqual(r.TL, { x: 30, y: 30, width: 19, height: 19 })
  assert.deepEqual(r.TR, { x: 745, y: 30, width: 19, height: 19 })
  assert.deepEqual(r.BL, { x: 30, y: 1074, width: 19, height: 19 })
  assert.deepEqual(r.BR, { x: 745, y: 1074, width: 19, height: 19 })
})

test('markRects: symmetric, inside the reserved bands, outside the objects area', () => {
  const w = 794
  const h = 1123
  for (const m of Object.values(markRects(w, h))) {
    // Same distance from the two edges of its corner (symmetry).
    const dx = Math.min(m.x, w - (m.x + m.width))
    const dy = Math.min(m.y, h - (m.y + m.height))
    assert.equal(dx, MARKS.INSET)
    assert.equal(dy, MARKS.INSET)
    // Entirely inside the band: never reaches the objects area.
    assert.ok(dx + m.width <= MARKS.BAND_W)
    assert.ok(dy + m.height <= MARKS.BAND_W)
  }
})

test('holeRect is centred in the distinct mark', () => {
  const br = markRects(794, 1123).BR
  const hole = holeRect(br)
  assert.deepEqual(hole, { x: 751, y: 1080, width: 7, height: 7 })
  assert.equal(hole.x - br.x, br.x + br.width - (hole.x + hole.width))
  assert.equal(hole.y - br.y, br.y + br.height - (hole.y + hole.height))
})

// ── Side marks — same values as alignment_marks ──────────────────────────────
test('SIDE_MARKS constants frozen (alignment_marks: SIDE_MARK_COUNT / SIDE_MARK_W / SIDE_MARK_H)', () => {
  assert.deepEqual({ ...SIDE_MARKS }, { COUNT: 17, W: 19, H: 8 })
})

test('sideMarkRects on the default A4 page matches the backend geometry', () => {
  const rows = sideMarkRects(794, 1123)
  assert.equal(rows.length, 17)
  assert.deepEqual(rows[0], {
    L: { x: 30, y: 93.5, width: 19, height: 8 },
    R: { x: 745, y: 93.5, width: 19, height: 8 },
  })
  assert.deepEqual(rows[8].L, { x: 30, y: 557.5, width: 19, height: 8 })
  assert.deepEqual(rows[16].R, { x: 745, y: 1021.5, width: 19, height: 8 })
  assert.deepEqual(rows.map(r => r.L.y), Array.from({ length: 17 }, (_, i) => 93.5 + 58 * i))
})

test('sideMarkRects: in the corner marks column, in the band, between the corner marks', () => {
  const w = 794
  const h = 1123
  const corners = markRects(w, h)
  const rows = sideMarkRects(w, h)
  for (const row of rows) {
    assert.equal(row.L.x, corners.TL.x)
    assert.equal(row.R.x, corners.TR.x)
    assert.equal(row.L.y, row.R.y)
    assert.ok(inReservedBand(row.L, w, h) && inReservedBand(row.R, w, h))
    assert.ok(row.L.x + row.L.width <= MARKS.BAND_W)
    assert.ok(row.R.x >= w - MARKS.BAND_W)
  }
  assert.ok(rows[0].L.y > corners.TL.y + corners.TL.height)
  assert.ok(rows[rows.length - 1].L.y + SIDE_MARKS.H < corners.BL.y)
})

// ── Usable area / reserved bands — same values as alignment_marks ────────────
test('usableRect on the default A4 page matches the backend geometry', () => {
  assert.deepEqual(usableRect(794, 1123), { x: 57, y: 57, width: 680, height: 1009 })
})

test('inReservedBand: an object on the edge of the usable area is valid', () => {
  assert.equal(inReservedBand({ x: 57, y: 57, width: 680, height: 1009 }, 794, 1123), false)
  assert.equal(inReservedBand({ x: 100, y: 200, width: 50, height: 40 }, 794, 1123), false)
})

test('inReservedBand: crossing any of the four bands is refused', () => {
  const w = 794
  const h = 1123
  assert.equal(inReservedBand({ x: 56, y: 200, width: 50, height: 40 }, w, h), true) // left
  assert.equal(inReservedBand({ x: 100, y: 56, width: 50, height: 40 }, w, h), true) // top
  assert.equal(inReservedBand({ x: 688, y: 200, width: 50, height: 40 }, w, h), true) // right: 738 > 737
  assert.equal(inReservedBand({ x: 100, y: 1027, width: 50, height: 40 }, w, h), true) // bottom: 1067 > 1066
})

test('inReservedBand: off the page counts as in a band', () => {
  assert.equal(inReservedBand({ x: 999, y: 200, width: 50, height: 40 }, 794, 1123), true)
  assert.equal(inReservedBand({ x: -10, y: 200, width: 50, height: 40 }, 794, 1123), true)
})
