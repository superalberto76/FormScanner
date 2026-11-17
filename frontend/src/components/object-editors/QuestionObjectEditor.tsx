// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useConfirm } from '../ConfirmProvider'
import type { QuestionType } from '../../api/domain'
import { isCorrectLocked } from '../../utils/questionSelection'
import type { OptionDraft, QuestionDraft } from './objectDraft'

interface Props {
  value: QuestionDraft
  onConfig: (patch: Partial<Pick<QuestionDraft, 'question_type' | 'question_text' | 'shuffle_answers'>>) => void
  onAddOption: () => void
  onUpdateOption: (cid: string, patch: Partial<Omit<OptionDraft, 'cid' | 'serverId'>>) => void
  onDeleteOption: (cid: string) => void
}

/**
 * Controlled QUESTION editor. Config + options live in the object
 * draft; this only renders inputs and forwards edits. Single-choice is
 * enforced locally by disabling sibling "correct" boxes once one is picked.
 */
export function QuestionObjectEditor({ value, onConfig, onAddOption, onUpdateOption, onDeleteOption }: Props) {
  const options = [...value.options].sort((a, b) => a.order_index - b.order_index)

  return (
    <div className="stack stack-12">
      <div className="stack stack-8">
        <div className="form-group">
          <label>Question type</label>
          <select value={value.question_type} onChange={e => onConfig({ question_type: e.target.value as QuestionType })}>
            <option value="SINGLE_CHOICE">SINGLE_CHOICE</option>
            <option value="MULTIPLE_CHOICE">MULTIPLE_CHOICE</option>
          </select>
        </div>
        <div className="form-group"><label>Question text</label><textarea rows={2} value={value.question_text} onChange={e => onConfig({ question_text: e.target.value })} /></div>
        <label className="nd-check"><input type="checkbox" checked={value.shuffle_answers} onChange={e => onConfig({ shuffle_answers: e.target.checked })} /> Shuffle answers</label>
      </div>

      <div className="stack stack-8">
        <strong>Options <span className="nd-muted">(✓ = correct)</span></strong>
        {options.length === 0 && <p className="nd-muted">No options yet.</p>}
        {options.map((o, idx) => (
          <OptionRow
            key={o.cid}
            option={o}
            locked={isCorrectLocked(value.question_type, options, idx)}
            onUpdate={onUpdateOption}
            onDelete={onDeleteOption}
          />
        ))}
        <button type="button" className="btn btn-secondary btn-sm" onClick={onAddOption}>+ Add option</button>
      </div>
    </div>
  )
}

function OptionRow({ option, locked, onUpdate, onDelete }: {
  option: OptionDraft
  locked: boolean
  onUpdate: (cid: string, patch: Partial<Omit<OptionDraft, 'cid' | 'serverId'>>) => void
  onDelete: (cid: string) => void
}) {
  const confirm = useConfirm()
  return (
    <div className={`nd-option-edit ${option.is_correct ? 'is-correct' : ''}`}>
      <div className="row row-8" style={{ alignItems: 'center' }}>
        <input style={{ flex: 1 }} value={option.option_text} placeholder="Option text" onChange={e => onUpdate(option.cid, { option_text: e.target.value })} />
        <label className="nd-check" title={locked ? 'Single choice: only one correct (clear the other first)' : 'Correct'}>
          <input type="checkbox" checked={option.is_correct} disabled={locked}
                 onChange={e => onUpdate(option.cid, { is_correct: e.target.checked })} /> ✓
        </label>
      </div>
      <div className="row row-8" style={{ alignItems: 'center' }}>
        <input type="number" step="any" style={{ width: 96 }} placeholder="score" value={option.score} onChange={e => onUpdate(option.cid, { score: e.target.value })} />
        <button type="button" className="btn btn-secondary btn-sm nd-danger-btn"
                onClick={async () => {
                  const ok = await confirm({
                    title: 'Remove option',
                    message: `Remove option "${option.option_text || '(empty)'}"? It is deleted from the server when you Save.`,
                    confirmLabel: 'Remove', danger: true,
                  })
                  if (ok) onDelete(option.cid)
                }}>Remove</button>
      </div>
    </div>
  )
}
