// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Canvas twin of the PDF-export layout constants.
//
// The authoring-canvas "faithful preview" must look like the printed PDF. Both
// sides therefore read the SAME numbers; this file mirrors, value-for-value, the
// constants that live in the backend export modules. EVERY export below cites
// its Python counterpart in a comment.
//
// ⚠️  ANTI-DIVERGENCE RULE: when a value changes, change BOTH sides together:
//     this file and the Python symbol it cites.
//     Freeze tests on both sides (exportLayout.test.ts + test_export_layout_freeze.py)
//     fail loudly if the two drift apart.

/**
 * Colours. The export uses fitz 0..1 RGB tuples; the canvas uses CSS strings.
 * 0.45 * 255 = 114.75 → 115; 0.72 * 255 = 183.6 → 184.
 * Python: app/utils/pdf_rendering.py — BLACK / GRAY / LIGHT / WHITE.
 */
export const COLORS = {
  BLACK: '#000', // BLACK = (0.0, 0.0, 0.0)
  GRAY: 'rgb(115,115,115)', // GRAY = (0.45, 0.45, 0.45)
  LIGHT: 'rgb(184,184,184)', // LIGHT = (0.72, 0.72, 0.72)
  WHITE: '#fff', // WHITE = (1.0, 1.0, 1.0) — hole of the distinct alignment mark only
} as const

/**
 * Map a font family to a CSS stack, replicating `fitz_font`'s substring buckets.
 * Python: app/utils/pdf_rendering.py — fitz_font(bold, italic, family):
 *   "cour"/"mono" → Courier · "times"/"serif" → Times · else → Helvetica.
 * Keep this detection byte-for-byte identical to the Python one.
 */
export function cssFontStack(family: string | null | undefined): string {
  const fam = (family ?? '').toLowerCase()
  if (fam.includes('cour') || fam.includes('mono')) return '"Courier New", monospace'
  if (fam.includes('times') || fam.includes('serif')) return '"Times New Roman", serif'
  return 'Arial, "Liberation Sans", sans-serif'
}

/**
 * Autofit (text shrink-to-fit). Mirrors `PageRenderer.text`:
 * start at the scaled size, shrink *0.9 until it fits, floor at 1.0; clip, no ellipsis.
 * Python: app/utils/pdf_rendering.py — PageRenderer.text (`fontsize * 0.9`, `_FLOOR = 1.0`).
 */
export const AUTOFIT = {
  SHRINK: 0.9, // fontsize = max(fontsize * 0.9, _FLOOR)
  FLOOR_PX: 1, // _FLOOR = 1.0
} as const

/**
 * QUESTION layout (pixel space).
 * Python: app/services/form_variant_export_service.py — _Q_* constants + the
 * header/option font sizes used in _render_question (12 bold header, 11 options).
 */
export const Q = {
  HEADER_H: 18, // _Q_HEADER_H = 18
  OPTION_H: 18, // _Q_OPTION_H = 18
  CIRCLE_R: 5, // _Q_CIRCLE_R = 5
  CIRCLE_CX: 9, // _Q_CIRCLE_CX = 9
  TEXT_X: 22, // _Q_TEXT_X = 22
  HEADER_SIZE: 12, // _render_question: r.text(..., 12, bold=True)
  OPTION_SIZE: 11, // _render_question: r.text(..., 11)
} as const

/**
 * MATRIX layout (pixel space).
 * Python: app/services/form_variant_export_service.py — _M_* constants + the
 * label_w ratio/cap and the font sizes used in _render_matrix (12 label, 9 headers/rows).
 * CIRCLE_R reuses _Q_CIRCLE_R (5) — the export shares the question option radius.
 */
export const M = {
  LABEL_H: 16, // _M_LABEL_H = 16
  HEADER_H: 16, // _M_HEADER_H = 16
  LABEL_W_RATIO: 0.25, // label_w = min(obj.width * 0.25, 120.0)
  LABEL_W_MAX: 120, // label_w = min(obj.width * 0.25, 120.0)
  CIRCLE_R: 5, // radius = _Q_CIRCLE_R = 5
  LABEL_SIZE: 12, // _render_matrix: r.text(..., 12, bold=True)
  HEADER_SIZE: 9, // _render_matrix: column headers r.text(..., 9, align=CENTER)
  ROW_LABEL_SIZE: 9, // _render_matrix: row labels r.text(..., 9)
} as const

/**
 * FREE_TEXT_AREA layout (pixel space).
 * Python: app/services/form_variant_export_service.py — _FT_LABEL_H + the sizes
 * and padding in _render_free_text (10 bold label, 9 italic placeholder, x+4/y+3, min(box_h,16)).
 */
export const FT = {
  LABEL_H: 14, // _FT_LABEL_H = 14
  LABEL_SIZE: 10, // _render_free_text: r.text(..., 10, bold=True)
  PLACEHOLDER_SIZE: 9, // _render_free_text: r.text(..., 9, italic=True)
  PAD_X: 4, // obj.x + 4 / obj.width - 8
  PAD_Y: 3, // box_y + 3
  PLACEHOLDER_MAX_H: 16, // min(box_h, 16)
} as const

/**
 * CODE placeholder (pixel space). The canvas has no encoder, so it ALWAYS draws
 * the placeholder (non-scannable by design).
 * Python: app/utils/code_rendering.py — _HUMAN_READABLE_STRIP_PX + the placeholder
 * text size 8 at (x+3, y+3) and the human-readable strip value size 8.
 */
export const CODE = {
  HUMAN_READABLE_STRIP_PX: 14, // _HUMAN_READABLE_STRIP_PX = 14
  TEXT_SIZE: 8, // render_code placeholder + HR strip: size 8
  PAD: 3, // x + 3, y + 3
} as const

/**
 * IMAGE placeholder.
 * Python: app/services/form_variant_export_service.py — _render_image_placeholder
 * draws the label at size 9.
 */
export const IMAGE_PLACEHOLDER = {
  LABEL_SIZE: 9, // _render_image_placeholder: r.text(..., 9, color=GRAY)
} as const

/**
 * Alignment marks and reserved bands (pixel space at 96 dpi).
 * Every exported page prints four marks, one per corner, drawn AFTER the objects;
 * the BR one has a centred WHITE hole so reading can tell the orientation.
 * Python: app/utils/alignment_marks.py — BAND_W / MARK_INSET / MARK_SIZE /
 * HOLE_SIZE / DISTINCT_CORNER, geometry in mark_rects() and hole_rect().
 */
export const MARKS = {
  BAND_W: 57, // BAND_W = 57 (15 mm)
  INSET: 30, // MARK_INSET = 30 (8 mm)
  SIZE: 19, // MARK_SIZE = 19 (5 mm)
  HOLE_SIZE: 7, // HOLE_SIZE = 7 (about 1/3 of MARK_SIZE)
  DISTINCT_CORNER: 'BR', // DISTINCT_CORNER = "BR"
} as const

export type MarkCorner = 'TL' | 'TR' | 'BL' | 'BR'

export interface MarkRect {
  x: number
  y: number
  width: number
  height: number
}

/** The four marks of a page, by corner. Python: alignment_marks.mark_rects. */
export function markRects(pageW: number, pageH: number): Record<MarkCorner, MarkRect> {
  const left = MARKS.INSET
  const top = MARKS.INSET
  const right = pageW - MARKS.INSET - MARKS.SIZE
  const bottom = pageH - MARKS.INSET - MARKS.SIZE
  const sq = (x: number, y: number): MarkRect => ({ x, y, width: MARKS.SIZE, height: MARKS.SIZE })
  return { TL: sq(left, top), TR: sq(right, top), BL: sq(left, bottom), BR: sq(right, bottom) }
}

/** The white hole of the distinct mark, centred in `mark`. Python: alignment_marks.hole_rect. */
export function holeRect(mark: MarkRect): MarkRect {
  const offset = (MARKS.SIZE - MARKS.HOLE_SIZE) / 2
  return { x: mark.x + offset, y: mark.y + offset, width: MARKS.HOLE_SIZE, height: MARKS.HOLE_SIZE }
}

/**
 * Side marks: a column of solid BLACK horizontal bars on each side,
 * in the column of the corner marks, centred on rows equally spaced between the
 * centre of the top corner mark and that of the bottom one (the corner marks are
 * the first and the last node). Reading straightens the sheet strip by strip on
 * them. Python: app/utils/alignment_marks.py — SIDE_MARK_COUNT / SIDE_MARK_W /
 * SIDE_MARK_H, geometry in side_mark_rects().
 */
export const SIDE_MARKS = {
  COUNT: 17, // SIDE_MARK_COUNT = 17
  W: 19, // SIDE_MARK_W = MARK_SIZE (5 mm)
  H: 8, // SIDE_MARK_H = 8 (2 mm)
} as const

export interface SideMarkRow {
  L: MarkRect
  R: MarkRect
}

/**
 * The side marks of a page, one row per marker top to bottom, each with the left
 * (`L`) and right (`R`) bar. Positions not rounded to the pixel (exactly 58 px
 * apart on the default A4 page). Python: alignment_marks.side_mark_rects.
 */
export function sideMarkRects(pageW: number, pageH: number): SideMarkRow[] {
  const top = MARKS.INSET + MARKS.SIZE / 2
  const bottom = pageH - MARKS.INSET - MARKS.SIZE / 2
  const step = (bottom - top) / (SIDE_MARKS.COUNT + 1)
  const left = MARKS.INSET
  const right = pageW - MARKS.INSET - SIDE_MARKS.W
  const rows: SideMarkRow[] = []
  for (let i = 1; i <= SIDE_MARKS.COUNT; i++) {
    const y = top + i * step - SIDE_MARKS.H / 2
    rows.push({
      L: { x: left, y, width: SIDE_MARKS.W, height: SIDE_MARKS.H },
      R: { x: right, y, width: SIDE_MARKS.W, height: SIDE_MARKS.H },
    })
  }
  return rows
}

/**
 * The area where objects go: the page minus the four reserved bands.
 * Python: alignment_marks.usable_rect.
 */
export function usableRect(pageW: number, pageH: number): MarkRect {
  return { x: MARKS.BAND_W, y: MARKS.BAND_W, width: pageW - 2 * MARKS.BAND_W, height: pageH - 2 * MARKS.BAND_W }
}

/**
 * Whether `rect` is not wholly inside `usableRect`, edges included: an object at
 * x = BAND_W is valid. Off the page counts as in a band too.
 * Python: alignment_marks.in_reserved_band.
 */
export function inReservedBand(rect: MarkRect, pageW: number, pageH: number): boolean {
  const u = usableRect(pageW, pageH)
  return (
    rect.x < u.x ||
    rect.y < u.y ||
    rect.x + rect.width > u.x + u.width ||
    rect.y + rect.height > u.y + u.height
  )
}
