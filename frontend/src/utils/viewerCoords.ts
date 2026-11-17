// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Pure, testable coordinate / sizing helpers for the variant page viewer.
// Extracted from VariantPageViewer/PageOverlay so the components hold layout/JSX
// and the math lives in one self-contained, unit-testable place.

import type { FormVariantPageRead } from '../api/domain'

/** A4 @ 96 DPI — the blank-page provisioning default. */
export const FALLBACK_PAGE_WIDTH = 794
export const FALLBACK_PAGE_HEIGHT = 1123
/** Cap the rendered "paper" width so native-small pages aren't upscaled hugely. */
export const MAX_SURFACE_PX = 760

export interface PageSize { width: number; height: number }
export interface Rect { x: number; y: number; width: number; height: number }
export interface PercentRect { leftPct: number; topPct: number; widthPct: number; heightPct: number }

/** Resolve a page's pixel coordinate space, falling back to A4 when unset. */
export function resolvePageSize(
  page: Pick<FormVariantPageRead, 'width_px' | 'height_px'>,
): PageSize {
  return {
    width: page.width_px ?? FALLBACK_PAGE_WIDTH,
    height: page.height_px ?? FALLBACK_PAGE_HEIGHT,
  }
}

/** Max rendered surface width in CSS px for a given page width. */
export function surfaceMaxWidthPx(pageWidth: number): number {
  return Math.min(pageWidth, MAX_SURFACE_PX)
}

/** Object rect (page px) → percentages of the page surface for CSS positioning. */
export function toPercentRect(rect: Rect, page: PageSize): PercentRect {
  const safeW = page.width || FALLBACK_PAGE_WIDTH
  const safeH = page.height || FALLBACK_PAGE_HEIGHT
  return {
    leftPct: (rect.x / safeW) * 100,
    topPct: (rect.y / safeH) * 100,
    widthPct: (rect.width / safeW) * 100,
    heightPct: (rect.height / safeH) * 100,
  }
}

/**
 * Convert a client (mouse/pointer) point into page-pixel coordinates, given the
 * surface's bounding rect. Pure and testable. Used by the editor for draw /
 * move / resize interactions.
 */
export function clientToPagePoint(
  clientX: number,
  clientY: number,
  surface: { left: number; top: number; width: number; height: number },
  page: PageSize,
): { x: number; y: number } {
  const fracX = surface.width === 0 ? 0 : (clientX - surface.left) / surface.width
  const fracY = surface.height === 0 ? 0 : (clientY - surface.top) / surface.height
  return { x: fracX * page.width, y: fracY * page.height }
}

/** Minimum object size (page px) enforced when drawing / resizing. */
export const MIN_OBJECT_SIZE = 8

/** Build a normalized (non-negative width/height) rect from two corner points. */
export function normalizeRect(
  a: { x: number; y: number },
  b: { x: number; y: number },
): Rect {
  return {
    x: Math.min(a.x, b.x),
    y: Math.min(a.y, b.y),
    width: Math.abs(a.x - b.x),
    height: Math.abs(a.y - b.y),
  }
}

/** Round a rect to integer page pixels. */
export function roundRect(r: Rect): Rect {
  return { x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height) }
}

/**
 * Clamp a rect inside `area` by translation (a move), keeping width/height >= MIN.
 * The size is first capped to the area's (a rect larger than the area shrinks to
 * it), then the position is shifted so the whole rect lies inside the area.
 * The editor passes the usable area of the page, i.e. the page minus the
 * reserved bands.
 */
export function clampRectToArea(rect: Rect, area: Rect, min = MIN_OBJECT_SIZE): Rect {
  const width = Math.max(min, Math.min(rect.width, area.width))
  const height = Math.max(min, Math.min(rect.height, area.height))
  const x = Math.min(Math.max(area.x, rect.x), Math.max(area.x, area.x + area.width - width))
  const y = Math.min(Math.max(area.y, rect.y), Math.max(area.y, area.y + area.height - height))
  return { x, y, width, height }
}

/**
 * Cut a rect to `area` edge by edge (a draw or a resize): an edge past
 * the area stops on it, the edges inside stay put, so the anchored corner of a
 * resize does not move. The result then goes through clampRectToArea, which only
 * matters when the cut leaves less than MIN (the rect is then pushed back inside).
 */
export function clipRectToArea(rect: Rect, area: Rect, min = MIN_OBJECT_SIZE): Rect {
  const left = Math.max(area.x, rect.x)
  const top = Math.max(area.y, rect.y)
  const right = Math.min(area.x + area.width, rect.x + rect.width)
  const bottom = Math.min(area.y + area.height, rect.y + rect.height)
  return clampRectToArea(
    { x: left, y: top, width: Math.max(0, right - left), height: Math.max(0, bottom - top) },
    area,
    min,
  )
}

/**
 * The edges of `area` that `rect` (the geometry asked for by the pointer, before
 * clamping) crosses: one guide line per edge the object is stopped on, drawn
 * during the drag.
 */
export function areaEdgeGuides(rect: Rect, area: Rect): SnapGuide[] {
  const guides: SnapGuide[] = []
  if (rect.x < area.x) guides.push({ orient: 'v', pos: area.x })
  if (rect.x + rect.width > area.x + area.width) guides.push({ orient: 'v', pos: area.x + area.width })
  if (rect.y < area.y) guides.push({ orient: 'h', pos: area.y })
  if (rect.y + rect.height > area.y + area.height) guides.push({ orient: 'h', pos: area.y + area.height })
  return guides
}

// ── Snapping ─────────────────────────────────────────────────────────────────
// Pure, composable snap helpers used by the editor's move/resize/draw paths.
// They only reshape the rect that is about to be committed; they never touch
// stored data. Two independent behaviours, combinable:
//   • grid   — latch coordinates to a fixed GRID_STEP lattice (page px).
//   • object — latch edges/centres to neighbouring objects within a threshold.
// Composition order is deterministic and documented on snapRect(): object-snap
// runs first (so edges latch to real neighbours), then grid fills the axes/size
// that object-snap left untouched.

/** Grid lattice step in page pixels. */
export const GRID_STEP = 25
/** Default object-snap catch radius in page pixels. */
export const SNAP_THRESHOLD = 6

/** A drawn alignment line: vertical (`v`, at x=`pos`) or horizontal (`h`, at y=`pos`). */
export interface SnapGuide { orient: 'v' | 'h'; pos: number }
/** A snapped rect plus the guide lines that were latched (for optional rendering). */
export interface SnapResult { rect: Rect; guides: SnapGuide[] }
/** Inputs for the composed snap: which behaviours are on, the neighbours, and tuning. */
export interface SnapSettings {
  grid: boolean
  object: boolean
  /** Other objects' rects (page px), excluding the one being edited. */
  others: Rect[]
  /** Grid-snap width/height too (resize/draw). When false (a move), size is preserved. */
  snapSize?: boolean
  step?: number
  threshold?: number
}

/** Round a single value to the nearest multiple of `step`. */
export function snapToStep(value: number, step = GRID_STEP): number {
  return Math.round(value / step) * step
}

/** Snap a rect's four edges to the grid; both corners land on the lattice. */
export function snapRectToGrid(rect: Rect, step = GRID_STEP): Rect {
  const x = snapToStep(rect.x, step)
  const y = snapToStep(rect.y, step)
  const right = snapToStep(rect.x + rect.width, step)
  const bottom = snapToStep(rect.y + rect.height, step)
  return { x, y, width: Math.max(step, right - x), height: Math.max(step, bottom - y) }
}

/** Candidate object-snap lines: each neighbour's near/centre/far edges per axis. */
function objectLines(others: Rect[]): { xs: number[]; ys: number[] } {
  const xs: number[] = []
  const ys: number[] = []
  for (const o of others) {
    xs.push(o.x, o.x + o.width / 2, o.x + o.width)
    ys.push(o.y, o.y + o.height / 2, o.y + o.height)
  }
  return { xs, ys }
}

/** Nearest (ref, line) pair within threshold; returns the delta to apply to a ref. */
function nearestLine(refs: number[], lines: number[], threshold: number): { delta: number; line: number } | null {
  let best: { delta: number; line: number } | null = null
  for (const r of refs) {
    for (const line of lines) {
      const delta = line - r
      if (Math.abs(delta) <= threshold && (best === null || Math.abs(delta) < Math.abs(best.delta))) {
        best = { delta, line }
      }
    }
  }
  return best
}

/**
 * Snap a rect to neighbouring objects by rigid translation: left/centre/right
 * (and top/centre/bottom) of the moving rect latch to the nearest neighbour line
 * within `threshold`. Size is preserved — only x/y shift — so it is the natural
 * model for a move/draw. Returns the snapped rect plus any latched guide lines.
 */
export function snapRectToObjects(rect: Rect, others: Rect[], threshold = SNAP_THRESHOLD): SnapResult {
  const { xs, ys } = objectLines(others)
  const refsX = [rect.x, rect.x + rect.width / 2, rect.x + rect.width]
  const refsY = [rect.y, rect.y + rect.height / 2, rect.y + rect.height]
  const sx = nearestLine(refsX, xs, threshold)
  const sy = nearestLine(refsY, ys, threshold)
  const guides: SnapGuide[] = []
  let { x, y } = rect
  if (sx) { x += sx.delta; guides.push({ orient: 'v', pos: sx.line }) }
  if (sy) { y += sy.delta; guides.push({ orient: 'h', pos: sy.line }) }
  return { rect: { ...rect, x, y }, guides }
}

/**
 * Snap a single point (the dragged corner of a resize) to neighbour lines, then
 * to the grid as a fallback on whichever axis didn't latch to an object. Keeping
 * this point-based means the anchored corner stays put during a resize.
 */
export function snapPoint(
  pt: { x: number; y: number },
  settings: SnapSettings,
): { point: { x: number; y: number }; guides: SnapGuide[] } {
  const step = settings.step ?? GRID_STEP
  const threshold = settings.threshold ?? SNAP_THRESHOLD
  let { x, y } = pt
  const guides: SnapGuide[] = []
  let xByObject = false
  let yByObject = false
  if (settings.object && settings.others.length > 0) {
    const { xs, ys } = objectLines(settings.others)
    const sx = nearestLine([x], xs, threshold)
    const sy = nearestLine([y], ys, threshold)
    if (sx) { x = sx.line; guides.push({ orient: 'v', pos: sx.line }); xByObject = true }
    if (sy) { y = sy.line; guides.push({ orient: 'h', pos: sy.line }); yByObject = true }
  }
  if (settings.grid) {
    if (!xByObject) x = snapToStep(x, step)
    if (!yByObject) y = snapToStep(y, step)
  }
  return { point: { x, y }, guides }
}

/**
 * Composed snap for a move/draw rect. Deterministic order: object-snap first
 * (rigid translation latching edges/centres to neighbours), then grid as the
 * fallback — grid only adjusts the axes object-snap left free, plus width/height
 * when `snapSize` is set (resize/draw). With both behaviours off this is the
 * identity, so the non-snapping editing path is unchanged.
 */
export function snapRect(rect: Rect, settings: SnapSettings): SnapResult {
  const step = settings.step ?? GRID_STEP
  const threshold = settings.threshold ?? SNAP_THRESHOLD
  const snapSize = settings.snapSize ?? true

  let out = rect
  const guides: SnapGuide[] = []
  let xByObject = false
  let yByObject = false

  if (settings.object && settings.others.length > 0) {
    const r = snapRectToObjects(out, settings.others, threshold)
    out = r.rect
    for (const g of r.guides) {
      guides.push(g)
      if (g.orient === 'v') xByObject = true
      else yByObject = true
    }
  }

  if (settings.grid) {
    let x = xByObject ? out.x : snapToStep(out.x, step)
    let y = yByObject ? out.y : snapToStep(out.y, step)
    let { width, height } = out
    if (snapSize) {
      const right = snapToStep(out.x + out.width, step)
      const bottom = snapToStep(out.y + out.height, step)
      width = Math.max(step, right - x)
      height = Math.max(step, bottom - y)
    }
    out = { x, y, width, height }
  }

  return { rect: out, guides }
}
