// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState, type KeyboardEvent } from 'react'
import type {
  PageObjectDetail,
  SubmissionReviewObject,
  SubstituteValue,
} from '../api/domain'
import {
  buildSubstituteValue,
  cellKey,
  detectedCells,
  detectedOptionIds,
  editorKind,
  initialEditorState,
  matrixHint,
  optionLetter,
  questionHint,
  type EditorHint,
  type EditorKind,
  type EditorState,
} from './submissionReview'

interface Props {
  obj: SubmissionReviewObject
  /** The authored object (GET /page-objects/{id}); undefined while it loads.
      Only a QUESTION or MATRIX needs it. */
  config: PageObjectDetail | undefined
  /** A save is in flight: every control is disabled. */
  busy: boolean
  /** A 422 for the value sent, shown under the editor, which stays open. */
  error: string | null
  onSave: (value: SubstituteValue) => void
  onCancel: () => void
}

const TITLE: Record<EditorKind, string> = {
  question: 'Replace the reading',
  matrix: 'Replace the reading',
  identifier: 'Replace the identifier',
  freeText: 'Replace the text',
  code: 'Replace the value',
}

/**
 * The replacement editor of a reading, chosen by
 * `editorKind`. Presentational: it holds only what is being typed and hands
 * the SUBSTITUTE `value` to `onSave`; the page sends it. The note is not here
 * — it belongs to the action area, and goes with whichever action is clicked.
 *
 * A QUESTION or MATRIX cannot be edited before its authored config arrives
 * (the options and the grid live there), so it shows a loading line until
 * then and its state starts from the config, never without it.
 *
 * Keys, handled here because they belong to the editor, not to
 * the page: Escape cancels; Enter saves from a checkbox or a single-line input
 * (identifier, code), Ctrl/⌘+Enter from anywhere — the free-text textarea
 * keeps plain Enter for a newline; in the question editor a letter toggles the
 * option it labels (A, B, C…) when focus is not in a text field. Opening puts
 * focus inside (the first checkbox, or the input); the matrix is mouse-only
 * for cells.
 */
export function SubmissionReviewEditor({ obj, config, busy, error, onSave, onCancel }: Props) {
  const kind = editorKind(obj)
  const needsConfig = kind === 'question' || kind === 'matrix'
  return (
    <div className="nd-review-editor">
      <div className="nd-review-actions-label">{TITLE[kind]}</div>
      {needsConfig && !config ? (
        <>
          <p className="nd-replace-hint">Loading the options…</p>
          <EditorButtons canSave={false} busy={busy} onSave={() => undefined} onCancel={onCancel} />
        </>
      ) : (
        <EditorBody
          kind={kind}
          obj={obj}
          config={config}
          busy={busy}
          onSave={onSave}
          onCancel={onCancel}
        />
      )}
      {error && <p className="error-msg nd-review-editor-error">{error}</p>}
    </div>
  )
}

function EditorBody({
  kind, obj, config, busy, onSave, onCancel,
}: {
  kind: EditorKind
  obj: SubmissionReviewObject
  config: PageObjectDetail | undefined
  busy: boolean
  onSave: (value: SubstituteValue) => void
  onCancel: () => void
}) {
  // Mounted once the config is there, so the initial state is built from it.
  const [state, setState] = useState<EditorState>(() => initialEditorState(obj, config))
  const required = kind === 'identifier' || kind === 'code'
  const canSave = !required || state.text.trim() !== ''
  const save = () => { if (canSave && !busy) onSave(buildSubstituteValue(kind, state)) }

  // The question's options in label order, for the letter keys.
  const options = kind === 'question'
    ? [...(config?.question_config?.options ?? [])].sort((a, b) => a.order_index - b.order_index)
    : []

  function onKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    const target = e.target as HTMLElement
    const tag = target.tagName
    const isCheckbox = tag === 'INPUT' && (target as HTMLInputElement).type === 'checkbox'
    const isTextField = (tag === 'INPUT' && !isCheckbox) || tag === 'TEXTAREA'
    const command = e.ctrlKey || e.metaKey
    if (e.key === 'Escape') {
      e.preventDefault()
      onCancel()
      return
    }
    if (e.key === 'Enter') {
      // A focused button keeps Enter for itself (Save, Cancel).
      if (tag === 'BUTTON' || e.altKey) return
      // Plain Enter in the textarea is a newline; elsewhere it saves.
      if (tag === 'TEXTAREA' && !command) return
      e.preventDefault()
      if (!e.repeat) save()
      return
    }
    if (kind === 'question' && !isTextField && !command && !e.altKey && /^[a-z]$/i.test(e.key)) {
      const index = e.key.toUpperCase().charCodeAt(0) - 65
      const option = options[index]
      if (!option || busy) return
      e.preventDefault()
      setState(s => {
        const next = new Set(s.selectedOptionIds)
        if (next.has(option.id)) next.delete(option.id)
        else next.add(option.id)
        return { ...s, selectedOptionIds: options.map(o => o.id).filter(i => next.has(i)) }
      })
    }
  }

  let body
  switch (kind) {
    case 'question':
      body = <QuestionEditor obj={obj} config={config!} state={state} setState={setState} busy={busy} />
      break
    case 'matrix':
      body = <MatrixEditor obj={obj} config={config!} state={state} setState={setState} busy={busy} />
      break
    case 'identifier':
      body = (
        <>
          <input
            type="text"
            className="nd-review-input nd-review-input-mono"
            aria-label="Identifier"
            value={state.text}
            maxLength={500}
            autoFocus
            disabled={busy}
            onChange={e => setState(s => ({ ...s, text: e.target.value }))}
          />
          <Hint hint={{ text: 'This becomes the submission’s candidate.', warn: false }} />
        </>
      )
      break
    case 'freeText':
      body = (
        <>
          <textarea
            className="nd-review-input"
            aria-label="Text"
            rows={3}
            value={state.text}
            maxLength={5000}
            autoFocus
            disabled={busy}
            onChange={e => setState(s => ({ ...s, text: e.target.value }))}
          />
          <Hint hint={{ text: 'Leave empty if the candidate wrote nothing.', warn: false }} />
        </>
      )
      break
    case 'code':
      body = (
        <input
          type="text"
          className="nd-review-input"
          aria-label="Code value"
          value={state.text}
          maxLength={2000}
          autoFocus
          disabled={busy}
          onChange={e => setState(s => ({ ...s, text: e.target.value }))}
        />
      )
      break
  }

  return (
    <div onKeyDown={onKeyDown}>
      {body}
      <EditorButtons canSave={canSave} busy={busy} onSave={save} onCancel={onCancel} />
    </div>
  )
}

function EditorButtons({
  canSave, busy, onSave, onCancel,
}: { canSave: boolean; busy: boolean; onSave: () => void; onCancel: () => void }) {
  return (
    <div className="nd-review-actions-row">
      <button type="button" className="btn btn-primary btn-sm" disabled={!canSave || busy} onClick={onSave}>
        Save replacement
      </button>
      <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={onCancel}>
        Cancel
      </button>
    </div>
  )
}

function Hint({ hint }: { hint: EditorHint }) {
  return (
    <p className={`nd-replace-hint${hint.warn ? ' nd-replace-hint-warn' : ''}`} aria-live="polite">
      {hint.text}
    </p>
  )
}

type SubEditorProps = {
  obj: SubmissionReviewObject
  config: PageObjectDetail
  state: EditorState
  setState: (update: (s: EditorState) => EditorState) => void
  busy: boolean
}

/** One checkbox per authored option, single- and multiple-choice alike: the
    operator transcribes what was marked, and the hint says what it counts as. */
function QuestionEditor({ obj, config, state, setState, busy }: SubEditorProps) {
  const question = config.question_config
  if (!question) return <p className="nd-replace-hint">This question has no options.</p>
  const options = [...question.options].sort((a, b) => a.order_index - b.order_index)
  const read = new Set(detectedOptionIds(obj))
  const selected = new Set(state.selectedOptionIds)
  const toggle = (id: string) =>
    setState(s => {
      const next = new Set(s.selectedOptionIds)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      // Kept in the question's option order, as the BE stores it.
      return { ...s, selectedOptionIds: options.map(o => o.id).filter(i => next.has(i)) }
    })
  return (
    <>
      <div className="nd-replace-options">
        {options.map((option, i) => (
          <label key={option.id} className={`nd-replace-option${read.has(option.id) ? ' is-read' : ''}`}>
            <input
              type="checkbox"
              checked={selected.has(option.id)}
              disabled={busy}
              autoFocus={i === 0}
              onChange={() => toggle(option.id)}
            />
            <span>{optionLetter(i)} · {option.option_text}</span>
            {read.has(option.id) && <span className="nd-replace-read-tag">read</span>}
          </label>
        ))}
      </div>
      <Hint hint={questionHint(question.question_type, state.selectedOptionIds.length)} />
      <p className="nd-replace-hint nd-keys-row">Press a letter to toggle an option</p>
    </>
  )
}

/** A checkbox per cell under the authored labels (1-based index fallback);
    the cells the extractor read are shaded. */
function MatrixEditor({ obj, config, state, setState, busy }: SubEditorProps) {
  const matrix = config.matrix_config
  if (!matrix || matrix.rows_count <= 0 || matrix.cols_count <= 0) {
    return <p className="nd-replace-hint">This matrix has no cells.</p>
  }
  const rowLabel = new Map(matrix.rows.map(r => [r.order_index, r.label]))
  const colLabel = new Map(matrix.cols.map(c => [c.order_index, c.label]))
  const rows = Array.from({ length: matrix.rows_count }, (_, i) => i)
  const cols = Array.from({ length: matrix.cols_count }, (_, i) => i)
  const read = detectedCells(obj)
  const toggle = (key: string) =>
    setState(s => {
      const next = new Set(s.cells)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return { ...s, cells: next }
    })
  return (
    <>
      <div className="nd-replace-grid-wrap">
        <table className="nd-replace-grid">
          <thead>
            <tr>
              <th />
              {cols.map(c => <th key={c} scope="col">{colLabel.get(c) ?? c + 1}</th>)}
            </tr>
          </thead>
          <tbody>
            {rows.map(r => (
              <tr key={r}>
                <th scope="row">{rowLabel.get(r) ?? r + 1}</th>
                {cols.map(c => {
                  const key = cellKey(r, c)
                  return (
                    <td key={c} className={read.has(key) ? 'is-read' : undefined}>
                      <input
                        type="checkbox"
                        aria-label={`${rowLabel.get(r) ?? `Row ${r + 1}`}, ${colLabel.get(c) ?? `Column ${c + 1}`}`}
                        checked={state.cells.has(key)}
                        disabled={busy}
                        autoFocus={r === 0 && c === 0}
                        onChange={() => toggle(key)}
                      />
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Hint hint={matrixHint(matrix.selection_rule, state.cells, matrix.rows_count, matrix.cols_count)} />
    </>
  )
}
