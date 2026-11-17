// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState, type FormEvent } from 'react'
import type { QuestionPageObjectCreate, QuestionType } from '../../api/domain'
import { isCorrectLocked } from '../../utils/questionSelection'
import { DEFAULT_GEOMETRY, GeometryFields, geometryToPayload, type GeometryState } from './GeometryFields'

interface Props {
  onSubmit: (data: QuestionPageObjectCreate) => void
  submitting?: boolean
  disabled?: boolean
  initialGeometry?: Partial<GeometryState>
}

interface OptionDraft { option_text: string; is_correct: boolean }

export function QuestionForm({ onSubmit, submitting = false, disabled = false, initialGeometry }: Props) {
  const [geo, setGeo] = useState<GeometryState>({ ...DEFAULT_GEOMETRY, width: 400, height: 160, ...initialGeometry })
  const [questionType, setQuestionType] = useState<QuestionType>('SINGLE_CHOICE')
  const [questionText, setQuestionText] = useState('')
  const [options, setOptions] = useState<OptionDraft[]>([
    { option_text: '', is_correct: false },
    { option_text: '', is_correct: false },
  ])

  function updateOption(i: number, patch: Partial<OptionDraft>) {
    setOptions(prev => prev.map((o, idx) => (idx === i ? { ...o, ...patch } : o)))
  }
  // Switching to SINGLE_CHOICE reconciles to a single correct option (keep the
  // first), mirroring the BE.
  function changeType(t: QuestionType) {
    setQuestionType(t)
    if (t === 'SINGLE_CHOICE') {
      setOptions(prev => {
        const first = prev.findIndex(o => o.is_correct)
        return prev.map((o, idx) => ({ ...o, is_correct: idx === first }))
      })
    }
  }
  function addOption() {
    setOptions(prev => [...prev, { option_text: '', is_correct: false }])
  }
  function removeOption(i: number) {
    setOptions(prev => prev.filter((_, idx) => idx !== i))
  }

  const validOptions = options.filter(o => o.option_text.trim())

  function submit(e: FormEvent) {
    e.preventDefault()
    onSubmit({
      ...geometryToPayload(geo),
      config: {
        question_type: questionType,
        question_text: questionText,
        options: validOptions.map((o, idx) => ({
          order_index: idx,
          option_text: o.option_text.trim(),
          is_correct: o.is_correct,
        })),
      },
    })
  }

  return (
    <form onSubmit={submit} className="stack stack-12">
      <GeometryFields value={geo} onChange={setGeo} />
      <div className="form-group">
        <label>Question type</label>
        <select value={questionType} onChange={e => changeType(e.target.value as QuestionType)}>
          <option value="SINGLE_CHOICE">SINGLE_CHOICE</option>
          <option value="MULTIPLE_CHOICE">MULTIPLE_CHOICE</option>
        </select>
      </div>
      <div className="form-group">
        <label>Question text</label>
        <textarea rows={2} value={questionText} onChange={e => setQuestionText(e.target.value)} required />
      </div>

      <div className="stack stack-8">
        <label>Options <span className="nd-muted">(tick = correct, used in SOLUTION PDF)</span></label>
        {options.map((o, i) => (
          <div key={i} className="row row-8" style={{ alignItems: 'center' }}>
            <input
              style={{ flex: 1 }}
              value={o.option_text}
              placeholder={`Option ${i + 1}`}
              onChange={e => updateOption(i, { option_text: e.target.value })}
            />
            <label className="nd-check" title={isCorrectLocked(questionType, options, i) ? 'Single choice: only one correct (clear the other first)' : 'Correct answer'}>
              <input type="checkbox" checked={o.is_correct}
                     disabled={isCorrectLocked(questionType, options, i)}
                     onChange={e => updateOption(i, { is_correct: e.target.checked })} /> ✓
            </label>
            <button type="button" className="btn btn-secondary btn-sm"
                    onClick={() => removeOption(i)} disabled={options.length <= 1}>✕</button>
          </div>
        ))}
        <button type="button" className="btn btn-secondary btn-sm" onClick={addOption}>+ Add option</button>
      </div>

      <button type="submit" className="btn btn-primary"
              disabled={disabled || submitting || !questionText.trim() || validOptions.length === 0}>
        {submitting ? 'Adding…' : 'Add QUESTION object'}
      </button>
    </form>
  )
}
