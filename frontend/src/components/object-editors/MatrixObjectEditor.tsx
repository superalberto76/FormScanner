// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useState } from 'react'
import { type MatrixObjectConfigRead, type MatrixSelectionRule } from '../../api/domain'
import { EditCellsModal, type AppliedCell } from '../EditCellsModal'
import { parseScore, type MatrixDraft } from './objectDraft'

const RULES: MatrixSelectionRule[] = ['ONE_PER_COLUMN', 'ONE_PER_ROW', 'MANY_PER_COLUMN', 'MANY_PER_ROW', 'FREE']

interface Props {
  value: MatrixDraft
  onConfig: (patch: Partial<{ selection_rule: MatrixSelectionRule; evaluative: boolean }>) => void
  onCounts: (rows: number, cols: number) => void
  onRowLabel: (cid: string, label: string) => void
  onColLabel: (cid: string, label: string) => void
  onCells: (cells: AppliedCell[]) => void
}

/**
 * Controlled MATRIX editor. rows/cols counts drive row/col labels
 * and the cell grid via the draft's auto-sync (no per-row Add/Delete):
 * raising a count appends Rn/Cn labels + default cells, lowering it trims the
 * trailing ones. All of it stays local until the panel's single Save.
 */
export function MatrixObjectEditor({ value, onConfig, onCounts, onRowLabel, onColLabel, onCells }: Props) {
  // Counts are committed on blur so partial typing (e.g. clearing the field)
  // can't transiently shrink the grid and drop labels/cells mid-keystroke.
  const [rowsInput, setRowsInput] = useState(String(value.rows_count))
  const [colsInput, setColsInput] = useState(String(value.cols_count))
  const [editingCells, setEditingCells] = useState(false)

  useEffect(() => { setRowsInput(String(value.rows_count)) }, [value.rows_count])
  useEffect(() => { setColsInput(String(value.cols_count)) }, [value.cols_count])

  function commitCounts() {
    const rows = Math.max(1, parseInt(rowsInput, 10) || 1)
    const cols = Math.max(1, parseInt(colsInput, 10) || 1)
    if (rows !== value.rows_count || cols !== value.cols_count) onCounts(rows, cols)
    else { setRowsInput(String(value.rows_count)); setColsInput(String(value.cols_count)) }
  }

  const rowDefs = [...value.rows].sort((a, b) => a.order_index - b.order_index)
  const colDefs = [...value.cols].sort((a, b) => a.order_index - b.order_index)

  // Synthetic read-config so the existing cell grid modal can edit the draft.
  const cellCfg: MatrixObjectConfigRead = {
    id: '', page_object_id: '',
    rows_count: value.rows_count, cols_count: value.cols_count,
    evaluative: value.evaluative, selection_rule: value.selection_rule,
    rows: rowDefs.map(r => ({ id: r.cid, matrix_object_config_id: '', order_index: r.order_index, label: r.label })),
    cols: colDefs.map(c => ({ id: c.cid, matrix_object_config_id: '', order_index: c.order_index, label: c.label })),
    cells: value.cells.map(c => ({
      id: c.serverId ?? '', matrix_object_config_id: '',
      row_index: c.row_index, col_index: c.col_index, is_correct: c.is_correct, score: parseScore(c.score),
    })),
  }

  return (
    <div className="stack stack-12">

      <div className="stack stack-8">
        <div className="nd-grid-2">
          <div className="form-group">
            <label>Rows</label>
            <input type="number" min={1} value={rowsInput}
                   onChange={e => setRowsInput(e.target.value)} onBlur={commitCounts} />
          </div>
          <div className="form-group">
            <label>Cols</label>
            <input type="number" min={1} value={colsInput}
                   onChange={e => setColsInput(e.target.value)} onBlur={commitCounts} />
          </div>
        </div>
        <div className="form-group">
          <label>Selection rule</label>
          <select value={value.selection_rule} onChange={e => onConfig({ selection_rule: e.target.value as MatrixSelectionRule })}>
            {RULES.map(r => <option key={r} value={r}>{r}</option>)}
          </select>
        </div>
        <label className="nd-check"><input type="checkbox" checked={value.evaluative} onChange={e => onConfig({ evaluative: e.target.checked })} /> Evaluative</label>
      </div>

      <div className="stack stack-8">
        <strong>Row labels</strong>
        {rowDefs.map(r => (
          <div key={r.cid} className="row row-8" style={{ alignItems: 'center' }}>
            <span className="nd-muted" style={{ width: 18 }}>{r.order_index}</span>
            <input style={{ flex: 1 }} value={r.label} onChange={e => onRowLabel(r.cid, e.target.value)} />
          </div>
        ))}
      </div>

      <div className="stack stack-8">
        <strong>Column labels</strong>
        {colDefs.map(c => (
          <div key={c.cid} className="row row-8" style={{ alignItems: 'center' }}>
            <span className="nd-muted" style={{ width: 18 }}>{c.order_index}</span>
            <input style={{ flex: 1 }} value={c.label} onChange={e => onColLabel(c.cid, e.target.value)} />
          </div>
        ))}
      </div>

      <div className="stack stack-8">
        <div className="row row-8" style={{ alignItems: 'center', justifyContent: 'space-between' }}>
          <strong>Cell definitions</strong>
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => setEditingCells(true)}>
            Edit cells
          </button>
        </div>
        <p className="nd-muted">{value.rows_count * value.cols_count} cell(s)</p>
      </div>

      {editingCells && (
        <EditCellsModal
          objectId=""
          cfg={cellCfg}
          onClose={() => setEditingCells(false)}
          onApplyCells={(cells) => { onCells(cells); setEditingCells(false) }}
        />
      )}
    </div>
  )
}
