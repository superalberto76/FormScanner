// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useState, type FormEvent } from 'react'
import type { MatrixCellDefCreate, MatrixPageObjectCreate, MatrixSelectionRule } from '../../api/domain'
import { normalizeApiError } from '../../utils/apiError'
import { isCorrectLocked, selectionGroupLabel } from '../../utils/matrixSelection'
import { DEFAULT_GEOMETRY, GeometryFields, geometryToPayload, type GeometryState } from './GeometryFields'

interface Props {
  onSubmit: (data: MatrixPageObjectCreate) => void
  submitting?: boolean
  disabled?: boolean
  initialGeometry?: Partial<GeometryState>
  /** Optional: surfaced as an inline error in the wizard footer (e.g. a 422). */
  error?: unknown
  /** Optional: a step-1 "Cancel" button that closes the host modal. */
  onCancel?: () => void
}

/** Per-cell editable draft, keyed by "rowIndex-colIndex". */
interface CellState { score: string; correct: boolean }

const SELECTION_RULES: MatrixSelectionRule[] = [
  'ONE_PER_ROW', 'ONE_PER_COLUMN', 'MANY_PER_ROW', 'MANY_PER_COLUMN', 'FREE',
]

export function MatrixForm({
  onSubmit, submitting = false, disabled = false, initialGeometry, error, onCancel,
}: Props) {
  const [step, setStep] = useState<1 | 2>(1)
  // Submit is "armed" only a moment after step 2 appears, so the trailing click
  // of a double-click on "Next" (which advances to step 2, where the submit
  // button then renders in the same spot) can't accidentally create the object.
  const [armed, setArmed] = useState(false)
  const [geo, setGeo] = useState<GeometryState>({ ...DEFAULT_GEOMETRY, width: 400, height: 220, ...initialGeometry })
  const [rowsCount, setRowsCount] = useState(2)
  const [colsCount, setColsCount] = useState(2)
  const [selectionRule, setSelectionRule] = useState<MatrixSelectionRule>('ONE_PER_ROW')
  const [evaluative, setEvaluative] = useState(false)
  const [rowLabels, setRowLabels] = useState<string[]>(['R1', 'R2'])
  const [colLabels, setColLabels] = useState<string[]>(['C1', 'C2'])
  // Score + correct for every cell, keyed by "ri-ci".
  const [cellGrid, setCellGrid] = useState<Record<string, CellState>>({})

  // Keep label arrays sized to the counts (truncate / pad with defaults).
  function syncLabels(count: number, labels: string[], prefix: string): string[] {
    const next = labels.slice(0, count)
    while (next.length < count) next.push(`${prefix}${next.length + 1}`)
    return next
  }
  function changeRows(n: number) {
    const c = Math.max(1, n || 1)
    setRowsCount(c)
    setRowLabels(prev => syncLabels(c, prev, 'R'))
  }
  function changeCols(n: number) {
    const c = Math.max(1, n || 1)
    setColsCount(c)
    setColLabels(prev => syncLabels(c, prev, 'C'))
  }

  // Rebuild the grid whenever the dimensions change, preserving values for cells
  // that still exist — so Back → edit rows/cols → Next keeps prior input and
  // drops only cells that fell out of range.
  useEffect(() => {
    setCellGrid(prev => {
      const next: Record<string, CellState> = {}
      for (let ri = 0; ri < rowsCount; ri++) {
        for (let ci = 0; ci < colsCount; ci++) {
          const key = `${ri}-${ci}`
          next[key] = prev[key] ?? { score: '', correct: false }
        }
      }
      return next
    })
  }, [rowsCount, colsCount])

  // Arm the submit button shortly after entering step 2 (covers the OS
  // double-click window). Re-arms each time step 2 is (re)entered via Back→Next.
  useEffect(() => {
    if (step !== 2) { setArmed(false); return }
    const id = window.setTimeout(() => setArmed(true), 400)
    return () => window.clearTimeout(id)
  }, [step])

  function setScore(key: string, score: string) {
    setCellGrid(prev => ({ ...prev, [key]: { ...(prev[key] ?? { score: '', correct: false }), score } }))
  }
  function toggleCorrect(key: string) {
    setCellGrid(prev => {
      const cur = prev[key] ?? { score: '', correct: false }
      const correct = !cur.correct
      // Checking "correct" on an empty score defaults it to 1; unchecking
      // leaves the score untouched.
      const score = correct && cur.score.trim() === '' ? '1' : cur.score
      return { ...prev, [key]: { score, correct } }
    })
  }

  const canAdvance = rowsCount >= 1 && colsCount >= 1

  function submit(e: FormEvent) {
    e.preventDefault()
    if (step !== 2 || !armed) return // only a deliberate step-2 submit creates
    // Materialise EVERY cell (rows × cols) so each bubble has a backing record,
    // not just the ones with a score/correct.
    const cells: MatrixCellDefCreate[] = []
    for (let ri = 0; ri < rowsCount; ri++) {
      for (let ci = 0; ci < colsCount; ci++) {
        const v = cellGrid[`${ri}-${ci}`] ?? { score: '', correct: false }
        cells.push({
          row_index: ri,
          col_index: ci,
          is_correct: v.correct,
          score: v.score.trim() !== '' ? parseFloat(v.score) : null,
        })
      }
    }
    onSubmit({
      ...geometryToPayload(geo),
      config: {
        rows_count: rowsCount,
        cols_count: colsCount,
        selection_rule: selectionRule,
        evaluative,
        rows: rowLabels.map((label, i) => ({ order_index: i, label })),
        cols: colLabels.map((label, i) => ({ order_index: i, label })),
        cells,
      },
    })
  }

  return (
    <form onSubmit={submit} className="stack stack-12">
      <p className="info-msg">
        Step {step} of 2 — {step === 1 ? 'Structure' : 'Cell definitions (optional)'}
      </p>

      {step === 1 && (
        <div className="stack stack-12">
          <GeometryFields value={geo} onChange={setGeo} />
          <div className="nd-grid-2">
            <div className="form-group">
              <label>Rows</label>
              <input type="number" min={1} value={rowsCount}
                     onChange={e => changeRows(parseInt(e.target.value, 10))} />
            </div>
            <div className="form-group">
              <label>Columns</label>
              <input type="number" min={1} value={colsCount}
                     onChange={e => changeCols(parseInt(e.target.value, 10))} />
            </div>
          </div>
          <div className="form-group">
            <label>Selection rule</label>
            <select value={selectionRule} onChange={e => setSelectionRule(e.target.value as MatrixSelectionRule)}>
              {SELECTION_RULES.map(r => <option key={r} value={r}>{r}</option>)}
            </select>
          </div>
          <label className="nd-check">
            <input type="checkbox" checked={evaluative} onChange={e => setEvaluative(e.target.checked)} /> Evaluative
          </label>

          <div className="stack stack-8">
            <label>Row labels</label>
            {rowLabels.map((l, i) => (
              <input key={i} value={l}
                     onChange={e => setRowLabels(prev => prev.map((v, idx) => idx === i ? e.target.value : v))} />
            ))}
          </div>
          <div className="stack stack-8">
            <label>Column labels</label>
            {colLabels.map((l, i) => (
              <input key={i} value={l}
                     onChange={e => setColLabels(prev => prev.map((v, idx) => idx === i ? e.target.value : v))} />
            ))}
          </div>
        </div>
      )}

      {step === 2 && (
        <div className="stack stack-8">
          <p className="nd-muted">Assign score and correct answer for each cell. You can skip this step.</p>
          <div style={{ overflowX: 'auto' }}>
            <table className="nd-table nd-table-compact">
              <thead>
                <tr>
                  <th />
                  {colLabels.map((cl, ci) => <th key={ci}>{cl}</th>)}
                </tr>
              </thead>
              <tbody>
                {rowLabels.map((rl, ri) => (
                  <tr key={ri}>
                    <th>{rl}</th>
                    {colLabels.map((_, ci) => {
                      const key = `${ri}-${ci}`
                      const cell = cellGrid[key] ?? { score: '', correct: false }
                      const locked = isCorrectLocked(cellGrid, selectionRule, key)
                      return (
                        <td key={ci} style={cell.correct ? { background: '#e6f7ea' } : undefined}>
                          <div className="stack stack-4">
                            <input type="number" step="any" value={cell.score} placeholder="score"
                                   style={{ width: 80 }}
                                   onChange={e => setScore(key, e.target.value)} />
                            <label className="nd-check">
                              <input type="checkbox" checked={cell.correct} disabled={locked}
                                     title={locked ? `Only one correct cell per ${selectionGroupLabel(selectionRule)} (${selectionRule})` : undefined}
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
        </div>
      )}

      {error != null && <p className="error-msg">{normalizeApiError(error)}</p>}

      <div className="row row-8">
        {step === 1 ? (
          <>
            {onCancel && (
              <button type="button" className="btn btn-secondary" onClick={onCancel}>Cancel</button>
            )}
            <button type="button" className="btn btn-primary" disabled={!canAdvance}
                    onClick={() => setStep(2)}>Next →</button>
          </>
        ) : (
          <>
            <button type="button" className="btn btn-secondary" onClick={() => setStep(1)}>← Back</button>
            <button type="submit" className="btn btn-primary" disabled={disabled || submitting || !armed}>
              {submitting ? 'Adding…' : 'Add MATRIX object'}
            </button>
          </>
        )}
      </div>
    </form>
  )
}
