// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import type { MatrixSelectionRule } from '../api/domain'

/** Editable cells keyed by "rowIndex-colIndex"; only the correct flag matters here. */
type CorrectGrid = Record<string, { correct: boolean }>

/**
 * Single-selection rules (ONE_PER_ROW / ONE_PER_COLUMN) allow at most one
 * "correct" cell per row / column. Returns true when the cell at `key` must
 * have its correct checkbox DISABLED because another cell in the same group is
 * already marked correct — i.e. the user is prevented from selecting a second
 * one and has to clear the current pick first.
 *
 * The already-correct cell itself is never locked, so it can always be toggled
 * off. No-op for MANY_PER_* / FREE (those allow many correct cells).
 *
 * Shared by the creation wizard (MatrixForm) and the edit grid (EditCellsModal)
 * so both enforce the same constraint identically.
 */
export function isCorrectLocked(
  grid: CorrectGrid,
  rule: MatrixSelectionRule,
  key: string,
): boolean {
  if (rule !== 'ONE_PER_ROW' && rule !== 'ONE_PER_COLUMN') return false
  if (grid[key]?.correct) return false
  const [ri, ci] = key.split('-').map(Number)
  for (const [k, v] of Object.entries(grid)) {
    if (k === key || !v.correct) continue
    const [r, c] = k.split('-').map(Number)
    const sameGroup = rule === 'ONE_PER_ROW' ? r === ri : c === ci
    if (sameGroup) return true
  }
  return false
}

/** Human-readable group name for tooltips/messages ("row" / "column"). */
export function selectionGroupLabel(rule: MatrixSelectionRule): 'row' | 'column' {
  return rule === 'ONE_PER_ROW' ? 'row' : 'column'
}
