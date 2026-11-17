// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Local draft model for the DRAFT object editor.
//
// The whole object lives in ONE local draft (presentation + typed config + children)
// that mirrors PageObjectDetail. Sections mutate the draft only; nothing hits
// the API until the single "Save" runs buildSavePlan() — a diff between the last
// saved snapshot (original) and the draft — and replays it through the existing
// granular nd.* endpoints.
//
// This file holds the pure pieces: the draft shape, buildDraft (detail → draft),
// syncMatrix (rows/cols/cells auto-sync when the counts change) and the diff
// planner + capture reconciliation used by the orchestrated commit.

import {
  nd,
  type CodeEncoding, type CodeType, type ImageFit,
  type MatrixObjectConfigRead, type MatrixSelectionRule,
  type PageObjectDetail, type QuestionObjectConfigRead, type QuestionType,
} from '../../api/domain'

// ── Draft shape ───────────────────────────────────────────────────────────────
export interface PresentationDraft {
  label: string
  description: string
  visible: boolean
  locked: boolean
}
export interface TextDraft {
  text: string; font_size: number; font_family: string; bold: boolean; italic: boolean
}
export interface CodeDraft {
  code_type: CodeType; encoding: CodeEncoding | null; value: string; human_readable: boolean
  include_page_index: boolean
}
export interface FreeTextDraft {
  // max_length kept as a string so the input can be cleared ('' = no limit);
  // parsed to a number | null only at commit time.
  placeholder: string; max_length: string
}
export interface OptionDraft {
  cid: string                 // stable client key (server id once persisted, else local-N)
  serverId: string | null     // null until created on the server
  order_index: number
  option_text: string
  is_correct: boolean
  score: string               // '' = no score
}
export interface QuestionDraft {
  question_type: QuestionType; question_text: string; shuffle_answers: boolean
  options: OptionDraft[]
}
export interface RowColDraft {
  cid: string
  serverId: string | null
  order_index: number
  label: string
}
export interface CellDraft {
  serverId: string | null     // null for cells materialised when the grid grows
  row_index: number
  col_index: number
  is_correct: boolean
  score: string               // '' = no score
}
export interface MatrixDraft {
  rows_count: number; cols_count: number
  selection_rule: MatrixSelectionRule; evaluative: boolean
  rows: RowColDraft[]; cols: RowColDraft[]; cells: CellDraft[]
}
// Only the draftable IMAGE property. The file itself is uploaded out-of-band
// (separate action), so it is NOT part of the single-Save draft.
export interface ImageDraft {
  fit: ImageFit
}
export interface ObjectDraft {
  presentation: PresentationDraft
  text?: TextDraft
  code?: CodeDraft
  freeText?: FreeTextDraft
  question?: QuestionDraft
  matrix?: MatrixDraft
  image?: ImageDraft
}

// Client-id generator for children added locally (before they exist on the
// server). A monotonic counter is enough — these never leave the browser.
let cidSeq = 0
export function newCid(): string { return `local-${++cidSeq}` }

/** '' / blank → null, otherwise the parsed float (NaN guarded to null). */
export function parseScore(s: string): number | null {
  const t = s.trim()
  if (t === '') return null
  const n = parseFloat(t)
  return Number.isNaN(n) ? null : n
}
/** Numeric equality of two raw input strings, treating blank/NaN as null. */
function numEq(a: string, b: string): boolean { return parseScore(a) === parseScore(b) }

// ── detail → draft ────────────────────────────────────────────────────────────
export function buildDraft(obj: PageObjectDetail): ObjectDraft {
  const d: ObjectDraft = {
    presentation: {
      label: obj.label ?? '',
      description: obj.description ?? '',
      visible: obj.visible,
      locked: obj.locked,
    },
  }
  if (obj.text_config) {
    const c = obj.text_config
    d.text = { text: c.text, font_size: c.font_size, font_family: c.font_family ?? '', bold: c.bold, italic: c.italic }
  }
  if (obj.code_config) {
    const c = obj.code_config
    d.code = {
      code_type: c.code_type,
      encoding: c.code_type === 'BARCODE' ? (c.encoding ?? 'CODE128') : null,
      value: c.value,
      human_readable: c.human_readable,
      include_page_index: c.include_page_index,
    }
  }
  if (obj.free_text_config) {
    const c = obj.free_text_config
    d.freeText = { placeholder: c.placeholder ?? '', max_length: c.max_length?.toString() ?? '' }
  }
  if (obj.question_config) d.question = buildQuestionDraft(obj.question_config)
  if (obj.matrix_config) d.matrix = buildMatrixDraft(obj.matrix_config)
  if (obj.image_config) d.image = { fit: obj.image_config.fit }
  return d
}

function buildQuestionDraft(c: QuestionObjectConfigRead): QuestionDraft {
  return {
    question_type: c.question_type,
    question_text: c.question_text,
    shuffle_answers: c.shuffle_answers,
    options: [...c.options].sort((a, b) => a.order_index - b.order_index).map(o => ({
      cid: o.id, serverId: o.id, order_index: o.order_index,
      option_text: o.option_text, is_correct: o.is_correct,
      score: o.score != null ? String(o.score) : '',
    })),
  }
}

function buildMatrixDraft(c: MatrixObjectConfigRead): MatrixDraft {
  return {
    rows_count: c.rows_count, cols_count: c.cols_count,
    selection_rule: c.selection_rule, evaluative: c.evaluative,
    rows: [...c.rows].sort((a, b) => a.order_index - b.order_index)
      .map(r => ({ cid: r.id, serverId: r.id, order_index: r.order_index, label: r.label })),
    cols: [...c.cols].sort((a, b) => a.order_index - b.order_index)
      .map(c2 => ({ cid: c2.id, serverId: c2.id, order_index: c2.order_index, label: c2.label })),
    cells: c.cells.map(cell => ({
      serverId: cell.id, row_index: cell.row_index, col_index: cell.col_index,
      is_correct: cell.is_correct, score: cell.score != null ? String(cell.score) : '',
    })),
  }
}

// ── MATRIX auto-sync ──────────────────────────────────────────────────────────
/**
 * Re-shape a matrix draft to `rows × cols`, in local state only:
 * - row/col defs: keep the ones still in range (preserving edited labels and
 *   server ids), drop the trailing ones, and materialise any missing index with
 *   the base label Rn / Cn (n = order_index + 1, e.g. a 3rd row → "R3").
 * - cells: keep every (r,c) still inside the grid (preserving values + server
 *   ids), drop the out-of-grid ones, and add the new ones as default cells so the
 *   grid stays fully materialised (every bubble has a backing record, matching
 *   the creation wizard). The diff turns these into the right create/delete calls.
 */
export function syncMatrix(m: MatrixDraft, rows: number, cols: number): MatrixDraft {
  const nextRows: RowColDraft[] = []
  for (let i = 0; i < rows; i++) {
    const existing = m.rows.find(r => r.order_index === i)
    nextRows.push(existing ?? { cid: newCid(), serverId: null, order_index: i, label: `R${i + 1}` })
  }
  const nextCols: RowColDraft[] = []
  for (let i = 0; i < cols; i++) {
    const existing = m.cols.find(c => c.order_index === i)
    nextCols.push(existing ?? { cid: newCid(), serverId: null, order_index: i, label: `C${i + 1}` })
  }
  const nextCells: CellDraft[] = []
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const existing = m.cells.find(x => x.row_index === r && x.col_index === c)
      nextCells.push(existing ?? { serverId: null, row_index: r, col_index: c, is_correct: false, score: '' })
    }
  }
  return { ...m, rows_count: rows, cols_count: cols, rows: nextRows, cols: nextCols, cells: nextCells }
}

// ── Save plan (diff original → draft) ─────────────────────────────────────────
/** A new server id captured after a successful create, fed back into the draft. */
export type Capture =
  | { kind: 'option'; cid: string; serverId: string }
  | { kind: 'row'; cid: string; serverId: string }
  | { kind: 'col'; cid: string; serverId: string }
  | { kind: 'cell'; row_index: number; col_index: number; serverId: string }

export interface SaveOp {
  label: string
  /** Runs the granular nd.* call; a create resolves to the capture to reconcile. */
  run: () => Promise<Capture | void>
}

/**
 * Diff the last-saved snapshot against the draft and return the ordered list of
 * granular operations that reconciles the server to the draft. Order encodes the
 * dependencies (creates before the things that use them, deletes last) and the
 * single-correct constraints: clears run before sets so a moved
 * "correct" flag never transiently doubles up and trips a 422.
 *
 * The list runs sequentially and stops at the first failure (see the panel's
 * commit). Because each step is an idempotent slice of the diff, re-running Save
 * after a partial failure applies only what is still missing.
 */
export function buildSavePlan(original: ObjectDraft, draft: ObjectDraft, objectId: string): SaveOp[] {
  const ops: SaveOp[] = []

  // 1) Presentation (geometry stays canvas-driven, so it is never
  //    part of the draft; only label/description/visible/locked are PATCHed).
  const p = draft.presentation, op = original.presentation
  if (p.label !== op.label || p.description !== op.description || p.visible !== op.visible || p.locked !== op.locked) {
    ops.push({ label: 'presentation', run: async () => { await nd.updatePageObject(objectId, {
      label: p.label.trim() || null,
      description: p.description.trim() || null,
      visible: p.visible,
      locked: p.locked,
    }) } })
  }

  // 2) Typed config
  if (draft.text && original.text) pushTextOps(ops, objectId, original.text, draft.text)
  if (draft.code && original.code) pushCodeOps(ops, objectId, original.code, draft.code)
  if (draft.freeText && original.freeText) pushFreeTextOps(ops, objectId, original.freeText, draft.freeText)
  if (draft.question && original.question) pushQuestionOps(ops, objectId, original.question, draft.question)
  if (draft.matrix && original.matrix) pushMatrixOps(ops, objectId, original.matrix, draft.matrix)
  if (draft.image && original.image) pushImageOps(ops, objectId, original.image, draft.image)

  return ops
}

function pushImageOps(ops: SaveOp[], id: string, o: ImageDraft, img: ImageDraft) {
  if (img.fit === o.fit) return
  ops.push({ label: 'image-config', run: async () => { await nd.updateImageConfig(id, { fit: img.fit }) } })
}

function pushTextOps(ops: SaveOp[], id: string, o: TextDraft, t: TextDraft) {
  if (t.text === o.text && t.font_size === o.font_size && t.font_family === o.font_family && t.bold === o.bold && t.italic === o.italic) return
  ops.push({ label: 'text-config', run: async () => { await nd.updateTextConfig(id, {
    text: t.text, font_size: t.font_size, font_family: t.font_family.trim() || null, bold: t.bold, italic: t.italic,
  }) } })
}

function pushCodeOps(ops: SaveOp[], id: string, o: CodeDraft, c: CodeDraft) {
  const enc = c.code_type === 'BARCODE' ? (c.encoding ?? 'CODE128') : null
  const oEnc = o.code_type === 'BARCODE' ? (o.encoding ?? 'CODE128') : null
  if (c.code_type === o.code_type && enc === oEnc && c.value === o.value && c.human_readable === o.human_readable && c.include_page_index === o.include_page_index) return
  ops.push({ label: 'code-config', run: async () => { await nd.updateCodeConfig(id, {
    code_type: c.code_type, encoding: enc, value: c.value, human_readable: c.human_readable, include_page_index: c.include_page_index,
  }) } })
}

function pushFreeTextOps(ops: SaveOp[], id: string, o: FreeTextDraft, f: FreeTextDraft) {
  if (f.placeholder === o.placeholder && numEq(f.max_length, o.max_length)) return
  ops.push({ label: 'free-text-config', run: async () => { await nd.updateFreeTextConfig(id, {
    placeholder: f.placeholder.trim() || null, max_length: parseScore(f.max_length),
  }) } })
}

function pushQuestionOps(ops: SaveOp[], id: string, orig: QuestionDraft, q: QuestionDraft) {
  if (q.question_type !== orig.question_type || q.question_text !== orig.question_text || q.shuffle_answers !== orig.shuffle_answers) {
    ops.push({ label: 'question-config', run: async () => { await nd.updateQuestionConfig(id, {
      question_type: q.question_type, question_text: q.question_text, shuffle_answers: q.shuffle_answers,
    }) } })
  }

  // Options: clears/neutral first, then deletes, then the "set correct" writes —
  // so single-choice never has two correct options live at once.
  const origById = new Map(orig.options.filter(o => o.serverId).map(o => [o.serverId as string, o]))
  const clears: SaveOp[] = []
  const setsCorrect: SaveOp[] = []
  const deletes: SaveOp[] = []

  for (const d of q.options) {
    if (d.serverId == null) {
      // Skip options the user added but never filled in — nothing to persist.
      if (d.option_text.trim() === '') continue
      const op: SaveOp = { label: 'create-option', run: async () => {
        const created = await nd.createQuestionOption(id, {
          order_index: d.order_index, option_text: d.option_text.trim(), is_correct: d.is_correct, score: parseScore(d.score),
        })
        return { kind: 'option', cid: d.cid, serverId: created.id }
      } }
      ;(d.is_correct ? setsCorrect : clears).push(op)
    } else {
      const o = origById.get(d.serverId)
      if (!o) continue
      if (o.option_text === d.option_text && o.is_correct === d.is_correct && numEq(o.score, d.score) && o.order_index === d.order_index) continue
      const sid = d.serverId
      const op: SaveOp = { label: 'update-option', run: async () => { await nd.updateQuestionOption(sid, {
        option_text: d.option_text.trim(), is_correct: d.is_correct, score: parseScore(d.score), order_index: d.order_index,
      }) } }
      ;(d.is_correct ? setsCorrect : clears).push(op)
    }
  }
  for (const o of orig.options) {
    if (o.serverId && !q.options.some(d => d.serverId === o.serverId)) {
      const sid = o.serverId
      deletes.push({ label: 'delete-option', run: async () => { await nd.deleteQuestionOption(sid) } })
    }
  }
  ops.push(...clears, ...deletes, ...setsCorrect)
}

function pushMatrixOps(ops: SaveOp[], id: string, orig: MatrixDraft, m: MatrixDraft) {
  // Config (incl. rows_count/cols_count) goes first so cell creates at new
  // indices are valid.
  if (m.rows_count !== orig.rows_count || m.cols_count !== orig.cols_count || m.selection_rule !== orig.selection_rule || m.evaluative !== orig.evaluative) {
    ops.push({ label: 'matrix-config', run: async () => { await nd.updateMatrixConfig(id, {
      rows_count: m.rows_count, cols_count: m.cols_count, selection_rule: m.selection_rule, evaluative: m.evaluative,
    }) } })
  }

  const rowCreates: SaveOp[] = [], rowUpdates: SaveOp[] = [], rowDeletes: SaveOp[] = []
  const colCreates: SaveOp[] = [], colUpdates: SaveOp[] = [], colDeletes: SaveOp[] = []

  const origRow = new Map(orig.rows.filter(r => r.serverId).map(r => [r.serverId as string, r]))
  for (const r of m.rows) {
    if (r.serverId == null) {
      rowCreates.push({ label: 'create-row', run: async () => {
        const created = await nd.createMatrixRow(id, { order_index: r.order_index, label: r.label })
        return { kind: 'row', cid: r.cid, serverId: created.id }
      } })
    } else {
      const o = origRow.get(r.serverId)
      if (o && (o.label !== r.label || o.order_index !== r.order_index)) {
        const sid = r.serverId
        rowUpdates.push({ label: 'update-row', run: async () => { await nd.updateMatrixRow(sid, { label: r.label, order_index: r.order_index }) } })
      }
    }
  }
  for (const o of orig.rows) {
    if (o.serverId && !m.rows.some(r => r.serverId === o.serverId)) {
      const sid = o.serverId
      rowDeletes.push({ label: 'delete-row', run: async () => { await nd.deleteMatrixRow(sid) } })
    }
  }

  const origCol = new Map(orig.cols.filter(c => c.serverId).map(c => [c.serverId as string, c]))
  for (const c of m.cols) {
    if (c.serverId == null) {
      colCreates.push({ label: 'create-col', run: async () => {
        const created = await nd.createMatrixCol(id, { order_index: c.order_index, label: c.label })
        return { kind: 'col', cid: c.cid, serverId: created.id }
      } })
    } else {
      const o = origCol.get(c.serverId)
      if (o && (o.label !== c.label || o.order_index !== c.order_index)) {
        const sid = c.serverId
        colUpdates.push({ label: 'update-col', run: async () => { await nd.updateMatrixCol(sid, { label: c.label, order_index: c.order_index }) } })
      }
    }
  }
  for (const o of orig.cols) {
    if (o.serverId && !m.cols.some(c => c.serverId === o.serverId)) {
      const sid = o.serverId
      colDeletes.push({ label: 'delete-col', run: async () => { await nd.deleteMatrixCol(sid) } })
    }
  }

  // Cells: split into clears (resulting not-correct, incl. new default cells) and
  // sets (resulting correct). Out-of-grid cells are deleted between the two so a
  // shrunk grid can't keep a stale "correct" that would block a new one.
  const cellClears: SaveOp[] = [], cellSets: SaveOp[] = [], cellDeletes: SaveOp[] = []
  for (const cell of m.cells) {
    const o = orig.cells.find(x => x.row_index === cell.row_index && x.col_index === cell.col_index)
    if (cell.serverId == null) {
      const op: SaveOp = { label: 'create-cell', run: async () => {
        const created = await nd.createMatrixCell(id, {
          row_index: cell.row_index, col_index: cell.col_index, is_correct: cell.is_correct, score: parseScore(cell.score),
        })
        return { kind: 'cell', row_index: cell.row_index, col_index: cell.col_index, serverId: created.id }
      } }
      ;(cell.is_correct ? cellSets : cellClears).push(op)
    } else if (o && (o.is_correct !== cell.is_correct || !numEq(o.score, cell.score))) {
      const sid = cell.serverId
      const op: SaveOp = { label: 'update-cell', run: async () => { await nd.updateMatrixCell(sid, { is_correct: cell.is_correct, score: parseScore(cell.score) }) } }
      ;(cell.is_correct ? cellSets : cellClears).push(op)
    }
  }
  for (const o of orig.cells) {
    if (o.serverId && (o.row_index >= m.rows_count || o.col_index >= m.cols_count)) {
      const sid = o.serverId
      cellDeletes.push({ label: 'delete-cell', run: async () => { await nd.deleteMatrixCell(sid) } })
    }
  }

  ops.push(
    ...rowCreates, ...colCreates,        // create defs first
    ...rowUpdates, ...colUpdates,        // relabel
    ...cellClears, ...cellDeletes, ...cellSets,  // clears → out-of-grid deletes → sets
    ...rowDeletes, ...colDeletes,        // drop trailing defs last
  )
}

// ── Capture reconciliation ────────────────────────────────────────────────────
/**
 * Fold the ids returned by successful creates back into the draft, so a retry
 * after a partial failure updates those children in place instead of creating
 * duplicates.
 */
export function applyCaptures(draft: ObjectDraft, captures: Capture[]): ObjectDraft {
  let d = draft
  for (const cap of captures) {
    if (cap.kind === 'option' && d.question) {
      d = { ...d, question: { ...d.question, options: d.question.options.map(o => o.cid === cap.cid ? { ...o, serverId: cap.serverId } : o) } }
    } else if (cap.kind === 'row' && d.matrix) {
      d = { ...d, matrix: { ...d.matrix, rows: d.matrix.rows.map(r => r.cid === cap.cid ? { ...r, serverId: cap.serverId } : r) } }
    } else if (cap.kind === 'col' && d.matrix) {
      d = { ...d, matrix: { ...d.matrix, cols: d.matrix.cols.map(c => c.cid === cap.cid ? { ...c, serverId: cap.serverId } : c) } }
    } else if (cap.kind === 'cell' && d.matrix) {
      d = { ...d, matrix: { ...d.matrix, cells: d.matrix.cells.map(c => c.row_index === cap.row_index && c.col_index === cap.col_index ? { ...c, serverId: cap.serverId } : c) } }
    }
  }
  return d
}
