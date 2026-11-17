// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Reducer hook backing the single-draft object editor.
//
// Owns the (original, draft) pair for one PageObject and exposes granular
// mutators the controlled section editors call on change/blur. Nothing here
// touches the API — buildSavePlan (objectDraft.ts) turns the draft into nd.*
// calls, and the panel orchestrates the commit.

import { useEffect, useMemo, useReducer } from 'react'
import type { PageObjectDetail } from '../../api/domain'
import {
  applyCaptures, buildDraft, buildSavePlan, newCid, syncMatrix,
  type Capture, type CodeDraft, type FreeTextDraft, type ImageDraft, type ObjectDraft,
  type OptionDraft, type PresentationDraft, type QuestionDraft, type SaveOp, type TextDraft,
} from './objectDraft'

/** A full cell grid handed back by the cell editor (score kept as raw string). */
export interface AppliedCell { row_index: number; col_index: number; is_correct: boolean; score: string }

interface State { original: ObjectDraft; draft: ObjectDraft; sourceId: string }

type Action =
  | { type: 'seed'; detail: PageObjectDetail }
  | { type: 'reset' }
  | { type: 'setPresentation'; patch: Partial<PresentationDraft> }
  | { type: 'setText'; patch: Partial<TextDraft> }
  | { type: 'setCode'; patch: Partial<CodeDraft> }
  | { type: 'setFreeText'; patch: Partial<FreeTextDraft> }
  | { type: 'setImage'; patch: Partial<ImageDraft> }
  | { type: 'setQuestionConfig'; patch: Partial<Pick<QuestionDraft, 'question_type' | 'question_text' | 'shuffle_answers'>> }
  | { type: 'addOption' }
  | { type: 'updateOption'; cid: string; patch: Partial<Omit<OptionDraft, 'cid' | 'serverId'>> }
  | { type: 'deleteOption'; cid: string }
  | { type: 'setMatrixConfig'; patch: Partial<{ selection_rule: MatrixRule; evaluative: boolean }> }
  | { type: 'setMatrixCounts'; rows: number; cols: number }
  | { type: 'updateRowLabel'; cid: string; label: string }
  | { type: 'updateColLabel'; cid: string; label: string }
  | { type: 'setCells'; cells: AppliedCell[] }
  | { type: 'commitResult'; captures: Capture[]; fresh: PageObjectDetail | null; failed: boolean }

// Local alias so the matrix-config action stays readable.
type MatrixRule = NonNullable<ObjectDraft['matrix']>['selection_rule']

function reducer(state: State | null, action: Action): State | null {
  if (action.type === 'seed') {
    return { original: buildDraft(action.detail), draft: buildDraft(action.detail), sourceId: action.detail.id }
  }
  if (!state) return state
  const { draft } = state

  switch (action.type) {
    case 'reset':
      return { ...state, draft: state.original }

    case 'setPresentation':
      return { ...state, draft: { ...draft, presentation: { ...draft.presentation, ...action.patch } } }

    case 'setText':
      return draft.text ? { ...state, draft: { ...draft, text: { ...draft.text, ...action.patch } } } : state

    case 'setCode':
      return draft.code ? { ...state, draft: { ...draft, code: { ...draft.code, ...action.patch } } } : state

    case 'setFreeText':
      return draft.freeText ? { ...state, draft: { ...draft, freeText: { ...draft.freeText, ...action.patch } } } : state

    case 'setImage':
      return draft.image ? { ...state, draft: { ...draft, image: { ...draft.image, ...action.patch } } } : state

    case 'setQuestionConfig': {
      if (!draft.question) return state
      const question: QuestionDraft = { ...draft.question, ...action.patch }
      // Switching to SINGLE_CHOICE must leave at most one correct option:
      // keep the first correct, clear the rest.
      if (action.patch.question_type === 'SINGLE_CHOICE') {
        let seen = false
        question.options = question.options.map(o => {
          if (!o.is_correct) return o
          if (seen) return { ...o, is_correct: false }
          seen = true
          return o
        })
      }
      return { ...state, draft: { ...draft, question } }
    }

    case 'addOption': {
      if (!draft.question) return state
      const next = draft.question.options.reduce((m, o) => Math.max(m, o.order_index), -1) + 1
      const option: OptionDraft = { cid: newCid(), serverId: null, order_index: next, option_text: '', is_correct: false, score: '' }
      return { ...state, draft: { ...draft, question: { ...draft.question, options: [...draft.question.options, option] } } }
    }

    case 'updateOption': {
      if (!draft.question) return state
      const options = draft.question.options.map(o => o.cid === action.cid ? { ...o, ...action.patch } : o)
      return { ...state, draft: { ...draft, question: { ...draft.question, options } } }
    }

    case 'deleteOption': {
      if (!draft.question) return state
      const options = draft.question.options.filter(o => o.cid !== action.cid)
      return { ...state, draft: { ...draft, question: { ...draft.question, options } } }
    }

    case 'setMatrixConfig':
      return draft.matrix ? { ...state, draft: { ...draft, matrix: { ...draft.matrix, ...action.patch } } } : state

    case 'setMatrixCounts':
      return draft.matrix ? { ...state, draft: { ...draft, matrix: syncMatrix(draft.matrix, action.rows, action.cols) } } : state

    case 'updateRowLabel': {
      if (!draft.matrix) return state
      const rows = draft.matrix.rows.map(r => r.cid === action.cid ? { ...r, label: action.label } : r)
      return { ...state, draft: { ...draft, matrix: { ...draft.matrix, rows } } }
    }

    case 'updateColLabel': {
      if (!draft.matrix) return state
      const cols = draft.matrix.cols.map(c => c.cid === action.cid ? { ...c, label: action.label } : c)
      return { ...state, draft: { ...draft, matrix: { ...draft.matrix, cols } } }
    }

    case 'setCells': {
      if (!draft.matrix) return state
      const prev = draft.matrix.cells
      const cells = action.cells.map(p => {
        const ex = prev.find(c => c.row_index === p.row_index && c.col_index === p.col_index)
        return { serverId: ex?.serverId ?? null, row_index: p.row_index, col_index: p.col_index, is_correct: p.is_correct, score: p.score }
      })
      return { ...state, draft: { ...draft, matrix: { ...draft.matrix, cells } } }
    }

    case 'commitResult': {
      const draftWithIds = applyCaptures(draft, action.captures)
      // No fresh detail (refetch failed): keep the ids we did capture so a retry
      // won't duplicate them, but leave the baseline untouched.
      if (!action.fresh) return { ...state, draft: draftWithIds }
      const seeded = buildDraft(action.fresh)
      // Realign the baseline to what is really persisted. On full success the
      // draft becomes that baseline; on partial failure we keep the user's draft
      // so the next Save replays only the residual diff.
      return action.failed
        ? { original: seeded, draft: draftWithIds, sourceId: action.fresh.id }
        : { original: seeded, draft: seeded, sourceId: action.fresh.id }
    }
  }
}

export interface ObjectDraftApi {
  ready: boolean
  draft: ObjectDraft | null
  plan: SaveOp[]
  dirty: boolean
  /** True when the pending plan changes the typed config/children (not only the
   *  presentation PATCH) — drives the shared-group resync on save. */
  configChanged: boolean
  actions: {
    reset: () => void
    setPresentation: (patch: Partial<PresentationDraft>) => void
    setText: (patch: Partial<TextDraft>) => void
    setCode: (patch: Partial<CodeDraft>) => void
    setFreeText: (patch: Partial<FreeTextDraft>) => void
    setImage: (patch: Partial<ImageDraft>) => void
    setQuestionConfig: (patch: Partial<Pick<QuestionDraft, 'question_type' | 'question_text' | 'shuffle_answers'>>) => void
    addOption: () => void
    updateOption: (cid: string, patch: Partial<Omit<OptionDraft, 'cid' | 'serverId'>>) => void
    deleteOption: (cid: string) => void
    setMatrixConfig: (patch: Partial<{ selection_rule: MatrixRule; evaluative: boolean }>) => void
    setMatrixCounts: (rows: number, cols: number) => void
    updateRowLabel: (cid: string, label: string) => void
    updateColLabel: (cid: string, label: string) => void
    setCells: (cells: AppliedCell[]) => void
  }
  commit: (captures: Capture[], fresh: PageObjectDetail | null, failed: boolean) => void
}

/**
 * Seed the draft from the loaded detail and expose the mutators + diff plan.
 * Re-seeds only when a different object id loads (a background refetch of the
 * same object — e.g. a canvas move — must not clobber in-flight edits); the
 * post-commit realignment is driven explicitly via `commit`.
 */
export function useObjectDraft(detail: PageObjectDetail | undefined, objectId: string): ObjectDraftApi {
  const [state, dispatch] = useReducer(reducer, null)

  useEffect(() => {
    if (detail) dispatch({ type: 'seed', detail })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail?.id])

  const actions = useMemo(() => ({
    reset: () => dispatch({ type: 'reset' }),
    setPresentation: (patch: Partial<PresentationDraft>) => dispatch({ type: 'setPresentation', patch }),
    setText: (patch: Partial<TextDraft>) => dispatch({ type: 'setText', patch }),
    setCode: (patch: Partial<CodeDraft>) => dispatch({ type: 'setCode', patch }),
    setFreeText: (patch: Partial<FreeTextDraft>) => dispatch({ type: 'setFreeText', patch }),
    setImage: (patch: Partial<ImageDraft>) => dispatch({ type: 'setImage', patch }),
    setQuestionConfig: (patch: Partial<Pick<QuestionDraft, 'question_type' | 'question_text' | 'shuffle_answers'>>) => dispatch({ type: 'setQuestionConfig', patch }),
    addOption: () => dispatch({ type: 'addOption' }),
    updateOption: (cid: string, patch: Partial<Omit<OptionDraft, 'cid' | 'serverId'>>) => dispatch({ type: 'updateOption', cid, patch }),
    deleteOption: (cid: string) => dispatch({ type: 'deleteOption', cid }),
    setMatrixConfig: (patch: Partial<{ selection_rule: MatrixRule; evaluative: boolean }>) => dispatch({ type: 'setMatrixConfig', patch }),
    setMatrixCounts: (rows: number, cols: number) => dispatch({ type: 'setMatrixCounts', rows, cols }),
    updateRowLabel: (cid: string, label: string) => dispatch({ type: 'updateRowLabel', cid, label }),
    updateColLabel: (cid: string, label: string) => dispatch({ type: 'updateColLabel', cid, label }),
    setCells: (cells: AppliedCell[]) => dispatch({ type: 'setCells', cells }),
  }), [])

  const plan = useMemo(
    () => state ? buildSavePlan(state.original, state.draft, objectId) : [],
    [state, objectId],
  )

  // The presentation PATCH is the only op that isn't a typed-config/children
  // change; anything else means the typed config moved.
  const configChanged = useMemo(
    () => plan.some(op => op.label !== 'presentation'),
    [plan],
  )

  const commit = useMemo(
    () => (captures: Capture[], fresh: PageObjectDetail | null, failed: boolean) =>
      dispatch({ type: 'commitResult', captures, fresh, failed }),
    [],
  )

  return {
    ready: state != null,
    draft: state?.draft ?? null,
    plan,
    dirty: plan.length > 0,
    configChanged,
    actions,
    commit,
  }
}
