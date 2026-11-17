// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useLayoutEffect, type RefObject } from 'react'
import { AUTOFIT } from '../utils/exportLayout'

interface AutofitOptions {
  /** Start font-size in PAGE pixels (e.g. cfg.font_size); scaled by pxScale here. */
  startPx: number
  /** Floor font-size in PAGE pixels (AUTOFIT.FLOOR_PX); scaled by pxScale here. */
  floorPx: number
  /** Page-px → CSS-px factor (uniform surface scale from the viewer). */
  pxScale: number
  /** Re-run the fit when any of these change (text, box dims, font flags, …). */
  deps: unknown[]
}

/**
 * Shrink-to-fit a text element, mirroring the PDF export's `PageRenderer.text`
 * autofit: start at `startPx * pxScale`, then while the content
 * overflows the box shrink by `AUTOFIT.SHRINK` (0.9) down to a floor of
 * `floorPx * pxScale`. The element clips its overflow (CSS `overflow:hidden`) and
 * never shows an ellipsis — the export truncates, so the canvas does too.
 *
 * Runs in `useLayoutEffect` so the measured/written size is committed before the
 * browser paints (no visible reflow flicker). The owning preview is memoized, so
 * only resized/edited objects re-run this — others stay put during a drag.
 */
export function useAutofitFontSize(
  ref: RefObject<HTMLElement | null>,
  { startPx, floorPx, pxScale, deps }: AutofitOptions,
): void {
  useLayoutEffect(() => {
    const el = ref.current
    if (!el || pxScale <= 0) return
    const floor = Math.max(floorPx * pxScale, 0.01)
    let size = Math.max(startPx * pxScale, floor)
    el.style.fontSize = `${size}px`
    // Shrink until it fits (height OR width) or we hit the floor — matching the
    // export, which retries `insert_textbox` at *0.9 until the overflow clears.
    let guard = 0
    while (
      (el.scrollHeight > el.clientHeight || el.scrollWidth > el.clientWidth) &&
      size > floor &&
      guard < 100
    ) {
      size = Math.max(size * AUTOFIT.SHRINK, floor)
      el.style.fontSize = `${size}px`
      guard += 1
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ref, startPx, floorPx, pxScale, ...deps])
}
