// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Pure, DOM-free decision logic for the faithful canvas preview.
// Kept out of PageOverlay so the export-mirroring rules are unit-testable without
// React, and so the component stays declarative. Numbers come from exportLayout.ts.

import type { PreviewMode } from '../hooks/useVariantWorkspaceState'

/**
 * Is a QUESTION option / MATRIX cell circle filled?
 * Mirrors the export: `filled = mode is SOLUTION and <is_correct>`. STUDENT never
 * fills; SOLUTION fills only the correct ones.
 */
export function isCircleFilled(previewMode: PreviewMode, isCorrect: boolean): boolean {
  return previewMode === 'SOLUTION' && isCorrect
}

/**
 * How many QUESTION options fit before the export clips. Mirrors _render_question:
 * options are laid out from `headerH` downward in `optionH` steps and the loop
 * BREAKS the first time a row would exceed the object's height
 * (`yy + optionH > obj.height`). Because optionH is constant the survivors are a
 * prefix, so this returns the count and the caller slices the sorted options.
 *
 * `headerH` / `optionH` are passed in (exportLayout `Q.HEADER_H` / `Q.OPTION_H`)
 * to keep this module free of runtime imports — it stays node-`--test`-runnable
 * with TS type-stripping, like the other pure preview helpers.
 */
export function visibleQuestionOptionCount(
  objectHeight: number,
  totalOptions: number,
  headerH: number,
  optionH: number,
): number {
  let count = 0
  let yy = headerH
  for (let i = 0; i < totalOptions; i++) {
    if (yy + optionH > objectHeight) break // clip overflow (no partial rows)
    count += 1
    yy += optionH
  }
  return count
}

/**
 * Resolve a MATRIX row/column label. Mirrors the export's
 * `labels.get(index, str(index))`: a defined label (even "") wins; only a MISSING
 * entry falls back to the index as a string.
 */
export function matrixLabel(labels: Map<number, string>, index: number): string {
  const label = labels.get(index)
  return label === undefined ? String(index) : label
}
