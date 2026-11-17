// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState } from 'react'
import { createPortal } from 'react-dom'
import { normalizeApiError } from '../utils/apiError'
import { isCorrectLocked, selectionGroupLabel } from '../utils/matrixSelection'
import { nd, type MatrixCellDefRead, type MatrixObjectConfigRead } from '../api/domain'

type CellState = { score: string; correct: boolean }

/** Build the editable grid from the persisted cells (keyed by "ri-ci"). */
function initGrid(
  rows: number,
  cols: number,
  existing: MatrixCellDefRead[],
): Record<string, CellState> {
  const grid: Record<string, CellState> = {}
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const found = existing.find(x => x.row_index === r && x.col_index === c)
      grid[`${r}-${c}`] = {
        score: found?.score != null ? String(found.score) : '',
        correct: found?.is_correct ?? false,
      }
    }
  }
  return grid
}

/** Full grid the modal hands back in draft mode (score kept as a raw string). */
export interface AppliedCell { row_index: number; col_index: number; is_correct: boolean; score: string }

export interface EditCellsModalProps {
  objectId: string
  cfg: MatrixObjectConfigRead
  onClose: () => void
  /** Persist-mode callback (per-cell API save). Ignored in draft/read-only mode. */
  onSaved?: () => void
  /**
   * Draft mode: when provided, the grid does NOT call the API. The
   * "Apply" button just hands the full edited grid back to the caller's draft,
   * which persists it later via the panel's single Save.
   */
  onApplyCells?: (cells: AppliedCell[]) => void
  /** View-only: inputs disabled, no save. Used by the read-only detail panel. */
  readOnly?: boolean
}

/**
 * Grid editor for MATRIX cell score + is_correct. Three modes:
 * - draft (onApplyCells): edits stay local, applied to the object draft;
 * - persist (onSaved): per-cell PATCH/POST diffed against cfg.cells (no bulk EP);
 * - read-only: just displays.
 */
export function EditCellsModal({ objectId, cfg, onClose, onSaved, onApplyCells, readOnly = false }: EditCellsModalProps) {
  const [cellGrid, setCellGrid] = useState<Record<string, CellState>>(() =>
    initGrid(cfg.rows_count, cfg.cols_count, cfg.cells),
  )
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const rows = [...cfg.rows].sort((a, b) => a.order_index - b.order_index)
  const cols = [...cfg.cols].sort((a, b) => a.order_index - b.order_index)

  function setScore(key: string, score: string) {
    setCellGrid(prev => ({ ...prev, [key]: { ...(prev[key] ?? { score: '', correct: false }), score } }))
  }
  function toggleCorrect(key: string) {
    setCellGrid(prev => {
      const cur = prev[key] ?? { score: '', correct: false }
      const correct = !cur.correct
      // Checking "correct" on an empty score defaults it to 1.
      const score = correct && cur.score === '' ? '1' : cur.score
      // selection_rule enforcement: under ONE_PER_ROW /
      // ONE_PER_COLUMN the checkboxes of sibling cells are DISABLED once one is
      // correct (see isCorrectLocked in the render), so the user can never reach
      // a second selection in the same group — no auto-deselect needed here.
      return { ...prev, [key]: { score, correct } }
    })
  }

  async function handleSave() {
    setSaving(true)
    setError(null)
    try {
      // A cell is NEVER deleted: every (row, col) is a bubble and must have a
      // backing record. So we UPDATE existing cells in place when score/correct
      // changed (including clearing back to none), and MATERIALISE any missing
      // cell (create persists is_correct + score). No delete.
      //
      // The BE rejects a 2nd correct cell in the same row/column with 422
      // (selection_rule), so when a correct flag is moved within a group we must
      // clear the old cell BEFORE marking the new one — otherwise the two
      // concurrent requests could transiently collide. Split into two phases:
      // clears/neutral writes first, then the new "correct" writes.
      const clearCalls: Promise<unknown>[] = []
      const setCorrectCalls: Promise<unknown>[] = []
      for (const [key, v] of Object.entries(cellGrid)) {
        const [ri, ci] = key.split('-').map(Number)
        const score = v.score !== '' ? parseFloat(v.score) : null
        const correct = v.correct
        const existing = cfg.cells.find(x => x.row_index === ri && x.col_index === ci)
        let call: Promise<unknown> | null = null
        if (existing) {
          if (existing.score !== score || existing.is_correct !== correct) {
            call = nd.updateMatrixCell(existing.id, { is_correct: correct, score })
          }
        } else {
          call = nd.createMatrixCell(objectId, { row_index: ri, col_index: ci, is_correct: correct, score })
        }
        if (call) (correct ? setCorrectCalls : clearCalls).push(call)
      }
      await Promise.all(clearCalls)
      await Promise.all(setCorrectCalls)
      onSaved?.()
      onClose()
    } catch (err) {
      setError(normalizeApiError(err))
    } finally {
      setSaving(false)
    }
  }

  // Draft mode: hand the full grid back to the caller (no API).
  function handleApply() {
    const cells: AppliedCell[] = []
    for (let r = 0; r < cfg.rows_count; r++) {
      for (let c = 0; c < cfg.cols_count; c++) {
        const v = cellGrid[`${r}-${c}`] ?? { score: '', correct: false }
        cells.push({ row_index: r, col_index: c, is_correct: v.correct, score: v.score })
      }
    }
    onApplyCells?.(cells)
    onClose()
  }

  // Portal to <body> so it covers the canvas overlays/handles.
  return createPortal(
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-box nd-modal-scroll" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h2>{readOnly ? 'Cells' : 'Edit cells'}</h2>
          <button type="button" className="btn btn-secondary btn-sm" onClick={onClose} disabled={saving}>✕</button>
        </div>
        <p className="nd-muted mb-16">
          {readOnly
            ? 'Score and correct answer for each cell.'
            : 'Assign score and correct answer for each cell.'}
        </p>
        <div style={{ overflowX: 'auto' }}>
          <table className="nd-table nd-table-compact">
            <thead>
              <tr>
                <th />
                {cols.map((col, ci) => <th key={col.id}>{col.label || `C${ci + 1}`}</th>)}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, ri) => (
                <tr key={row.id}>
                  <th>{row.label || `R${ri + 1}`}</th>
                  {cols.map((_, ci) => {
                    const key = `${ri}-${ci}`
                    const cell = cellGrid[key] ?? { score: '', correct: false }
                    const locked = !readOnly && isCorrectLocked(cellGrid, cfg.selection_rule, key)
                    return (
                      <td key={ci} style={cell.correct ? { background: '#e6f7ea' } : undefined}>
                        <div className="stack stack-4">
                          <input type="number" step="any" value={cell.score} placeholder="score"
                                 style={{ width: 80 }} disabled={readOnly || saving}
                                 onChange={e => setScore(key, e.target.value)} />
                          <label className="nd-check">
                            <input type="checkbox" checked={cell.correct} disabled={readOnly || saving || locked}
                                   title={locked ? `Only one correct cell per ${selectionGroupLabel(cfg.selection_rule)} (${cfg.selection_rule})` : undefined}
                                   onChange={() => toggleCorrect(key)} /> correct
                          </label>
                        </div>
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {error && <p className="error-msg">{error}</p>}
        <div className="row row-8">
          {readOnly ? (
            <button type="button" className="btn btn-secondary" onClick={onClose}>Close</button>
          ) : onApplyCells ? (
            <>
              <button type="button" className="btn btn-secondary" onClick={onClose}>Cancel</button>
              <button type="button" className="btn btn-primary" onClick={handleApply}>Apply</button>
            </>
          ) : (
            <>
              <button type="button" className="btn btn-secondary" onClick={onClose} disabled={saving}>Cancel</button>
              <button type="button" className="btn btn-primary" onClick={handleSave} disabled={saving}>
                {saving ? 'Saving…' : 'Save cells'}
              </button>
            </>
          )}
        </div>
      </div>
    </div>,
    document.body,
  )
}
