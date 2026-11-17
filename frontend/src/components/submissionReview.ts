// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Pure helpers for the submission review page: reason chips and
// their explanations, the proposed reading as text, list titles, the default
// selection and the lock state the page is in — plus the geometry of the scan
// crop in the focus panel, the review actions' editors, hints,
// outcome texts and "next unresolved", the page's keyboard map,
// and closing the submission and reopening it.
// Same split as batchSubmissions.ts: a plain .ts module (no JSX) so every rule
// can be unit-tested with Node's native runner (`node --test`) — see
// submissionReview.test.ts. Only `import type` from the app runtime: the page
// and its two components hold the markup, the rules live here.
import type {
  MatrixSelectionRule,
  PageObjectDetail,
  QuestionType,
  ReviewOutcome,
  SubmissionLockInfo,
  SubmissionResultType,
  SubmissionReviewDetail,
  SubmissionReviewGeometry,
  SubmissionReviewObject,
  SubmissionReviewPage,
  SubmissionReviewTemplatePage,
  SubmissionStatus,
  SubstituteValue,
} from '../api/domain'
// The explicit .ts extension is what lets Node's type-stripping test runner
// resolve this runtime import; Vite and tsc (allowImportingTsExtensions) accept it.
import { formatThresholdPercent } from './batchQueue.ts'
import { isClosedStatus, showNoCandidate } from './batchSubmissions.ts'
import {
  FALLBACK_PAGE_HEIGHT,
  FALLBACK_PAGE_WIDTH,
  toPercentRect,
  type PageSize,
  type PercentRect,
} from '../utils/viewerCoords.ts'

// ── Flag reasons ─────────────────────────────────────────────────────────────

const REASON_LABEL: Record<string, string> = {
  LOW_CONFIDENCE: 'Low confidence',
  INVALID_ANSWER: 'Invalid answer',
  ALWAYS_REVIEW: 'Always reviewed',
  DECODE_FAILED: 'Code not decoded',
  AMBIGUOUS_DIGITS: 'Ambiguous digits',
  MISSING_DIGITS: 'Missing digits',
  UNREADABLE_GRID: 'Grid not readable',
  EXTRACTION_FAILED: 'Could not be read',
  SHEET_NOT_ALIGNED: 'Sheet not aligned',
}

/**
 * The chip text of a flag reason. A code the FE does not know yet (the BE grew
 * one) is shown raw rather than hidden: an unexplained reason is still a
 * reason, and dropping it would make the reading look flagged for nothing.
 */
export function reasonLabel(code: string): string {
  return REASON_LABEL[code] ?? code
}

/* Amber for "unsure" (the reading may well be right), red for "the reading is
   broken" (it cannot be right as it stands), blue for "by design" (nothing went
   wrong; this type is always looked at). */
const REASON_CHIP_CLASS: Record<string, string> = {
  LOW_CONFIDENCE: 'nd-chip-warning',
  SHEET_NOT_ALIGNED: 'nd-chip-warning',
  INVALID_ANSWER: 'nd-chip-danger',
  DECODE_FAILED: 'nd-chip-danger',
  AMBIGUOUS_DIGITS: 'nd-chip-danger',
  MISSING_DIGITS: 'nd-chip-danger',
  UNREADABLE_GRID: 'nd-chip-danger',
  EXTRACTION_FAILED: 'nd-chip-danger',
  ALWAYS_REVIEW: 'nd-chip-info',
}

/** The chip's colour class. Unknown codes are muted: claiming a severity for a
    reason the FE cannot interpret would be a guess. */
export function reasonChipClass(code: string): string {
  return REASON_CHIP_CLASS[code] ?? 'nd-chip-muted'
}

/**
 * One sentence for "Why it is here". `thresholdFraction` is the batch's
 * effective threshold as the BE sends it (0.8), shown as a percent. Unknown
 * codes give "" — the chip already shows the raw code, and an invented
 * explanation would be worse than none.
 */
export function reasonExplanation(code: string, thresholdFraction: number): string {
  switch (code) {
    case 'LOW_CONFIDENCE':
      return `Read with low confidence (below the ${formatThresholdPercent(thresholdFraction)} threshold).`
    case 'INVALID_ANSWER':
      return 'The marks do not respect the question’s rules, e.g. two answers on a single-choice question.'
    case 'ALWAYS_REVIEW':
      return 'Handwritten text is always checked by a person.'
    case 'DECODE_FAILED':
      return 'The printed code could not be decoded.'
    case 'AMBIGUOUS_DIGITS':
      return 'More than one digit is marked in at least one column.'
    case 'MISSING_DIGITS':
      return 'At least one column has no digit marked.'
    case 'UNREADABLE_GRID':
      return 'The identifier grid could not be read.'
    case 'EXTRACTION_FAILED':
      return 'This answer could not be read from the scan. Enter it from the paper sheet.'
    case 'SHEET_NOT_ALIGNED':
      return 'This sheet could not be aligned to the template, so the reading may be shifted. Check it against the full sheet.'
    default:
      return ''
  }
}

// ── Proposed reading ─────────────────────────────────────────────────────────

/** "A", "B", … "Z", then "AA", "AB" … — a 0-based option position as the letter
    printed next to its bubble. */
export function optionLetter(index: number): string {
  let n = index
  let letters = ''
  do {
    letters = String.fromCharCode(65 + (n % 26)) + letters
    n = Math.floor(n / 26) - 1
  } while (n >= 0)
  return letters
}

function percentOf(confidence: unknown): string | null {
  return typeof confidence === 'number' ? `${Math.round(confidence * 100)}%` : null
}

/** " (96%)" after a mark, or nothing when the confidence is missing. */
function percent(confidence: unknown): string {
  const p = percentOf(confidence)
  return p ? ` (${p})` : ''
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function records(value: unknown): Record<string, unknown>[] | null {
  return Array.isArray(value) && value.every(isRecord) ? value : null
}

const NO_READING = ['No reading available']
// A "Review anyway" placeholder: no run could read it, and that is the point —
// the operator is expected to type the answer in, not to wait for one.
const PLACEHOLDER_READING = ['No reading: enter it from the paper sheet']

function questionLines(
  payload: Record<string, unknown>,
  config: PageObjectDetail | undefined,
): string[] | null {
  const selected = records(payload.selected_options)
  if (!selected) return null
  if (selected.length === 0) return ['No option marked']
  const options = config?.question_config?.options
  if (!options) {
    // The payload names options by id only, so without the authored options
    // there is no position to show — the confidences are all that is known.
    const what = selected.length === 1 ? '1 option marked' : `${selected.length} options marked`
    const confidences = selected.map(s => percentOf(s.confidence)).filter(p => p !== null)
    return [confidences.length > 0 ? `${what} (${confidences.join(', ')})` : what]
  }
  const ordered = [...options].sort((a, b) => a.order_index - b.order_index)
  const position = new Map(ordered.map((o, i) => [o.id, i]))
  const parts = selected
    .map(s => ({ index: position.get(String(s.option_id)), confidence: s.confidence }))
    // Bubble order, as on the sheet; an option no longer authored ("?") last.
    .sort((a, b) => (a.index ?? Infinity) - (b.index ?? Infinity))
    .map(p => `${p.index === undefined ? '?' : optionLetter(p.index)}${percent(p.confidence)}`)
  return [parts.join(', ')]
}

function matrixLines(
  payload: Record<string, unknown>,
  config: PageObjectDetail | undefined,
): string[] | null {
  const rows = records(payload.rows) ?? []
  const cols = records(payload.cols) ?? []
  if (!Array.isArray(payload.rows) && !Array.isArray(payload.cols)) return null
  const matrix = config?.matrix_config
  const rowLabel = new Map((matrix?.rows ?? []).map(r => [r.order_index, r.label]))
  const colLabel = new Map((matrix?.cols ?? []).map(c => [c.order_index, c.label]))
  // Without the config, 1-based indices: the positions an operator counts on
  // the sheet.
  const nameRow = (i: unknown) => rowLabel.get(Number(i)) ?? `Row ${Number(i) + 1}`
  const nameCol = (i: unknown) => colLabel.get(Number(i)) ?? `Column ${Number(i) + 1}`
  const cellRow = (i: unknown) => rowLabel.get(Number(i)) ?? String(Number(i) + 1)
  const cellCol = (i: unknown) => colLabel.get(Number(i)) ?? String(Number(i) + 1)

  const byRow = rows.length > 0
  const groups = byRow ? rows : cols
  if (groups.length === 0) return null
  return groups.map(group => {
    const name = byRow ? nameRow(group.row_index) : nameCol(group.col_index)
    const cells = records(group.selected_cells) ?? []
    if (cells.length === 0) return `${name}: no mark`
    const marks = cells.map(c =>
      `${byRow ? cellCol(c.col_index) : cellRow(c.row_index)}${percent(c.confidence)}`,
    )
    return `${name}: ${marks.join(', ')}`
  })
}

function identifierLines(payload: Record<string, unknown>): string[] | null {
  const raw = payload.raw_value
  // An empty raw_value is the BE's unreadable grid: nothing was read at all.
  if (typeof raw !== 'string' || raw === '') return null
  const lines = [raw]
  const ambiguous = Array.isArray(payload.ambiguous_columns)
    ? payload.ambiguous_columns.filter((c): c is number => typeof c === 'number')
    : []
  if (ambiguous.length > 0) {
    lines.push(`Ambiguous columns: ${ambiguous.map(c => c + 1).join(', ')}`)
  }
  return lines
}

function freeTextLines(payload: Record<string, unknown>): string[] | null {
  const text = payload.suggested_text
  if (typeof text !== 'string') return null
  return [text.trim() === '' ? 'Nothing legible' : `“${text}”`]
}

function codeLines(payload: Record<string, unknown>): string[] | null {
  const value = payload.decoded_value
  if (payload.decode_successful === true && typeof value === 'string') return [value]
  if (payload.decode_successful === false || value === null) return ['Not decoded']
  return null
}

/**
 * The extractor's reading of `obj`, as one or more lines of text. `config` is
 * the authored object (GET /page-objects/{id}) once loaded: it turns option ids
 * into letters and matrix indices into labels. Without it the reading is still
 * shown, with indices instead of labels. A null or unexpected payload gives
 * "No reading available" rather than throwing — the payload is a JSON blob the
 * FE does not validate — except a placeholder (null payload, flagged
 * EXTRACTION_FAILED), which says what to do about it instead.
 */
export function formatProposedReading(
  obj: Pick<SubmissionReviewObject, 'result_type' | 'detected_payload'> &
    Partial<Pick<SubmissionReviewObject, 'flag_reasons'>>,
  config?: PageObjectDetail,
): string[] {
  const payload = obj.detected_payload
  if (payload === null && obj.flag_reasons?.includes('EXTRACTION_FAILED')) {
    return PLACEHOLDER_READING
  }
  if (!isRecord(payload)) return NO_READING
  let lines: string[] | null
  switch (obj.result_type) {
    case 'QUESTION': lines = questionLines(payload, config); break
    case 'MATRIX': lines = matrixLines(payload, config); break
    case 'RESPONDENT_IDENTIFIER': lines = identifierLines(payload); break
    case 'FREE_TEXT': lines = freeTextLines(payload); break
    case 'CODE': lines = codeLines(payload); break
    default: lines = null
  }
  return lines ?? NO_READING
}

// ── Titles ───────────────────────────────────────────────────────────────────

/** "Page 2 · sheet 3" — the template page first, since that is what the
    operator holds in their head; "Unplaced sheet 3" when matching placed none. */
export function pageGroupTitle(
  page: Pick<SubmissionReviewPage, 'source_page_order' | 'template_page'>,
): string {
  const sheet = page.source_page_order + 1
  return page.template_page
    ? `Page ${page.template_page.page_index} · sheet ${sheet}`
    : `Unplaced sheet ${sheet}`
}

/** The warning under a sheet's group title: only for a
    sheet the extraction examined and could not align. A sheet aligned, or
    never examined for alignment, has none. "sheet N" is the group
    title's own numbering, so both name the same sheet. */
export function pageAlignmentWarning(
  page: Pick<SubmissionReviewPage, 'source_page_order' | 'alignment_status'>,
): string | null {
  if (page.alignment_status !== 'NOT_ALIGNED') return null
  const sheet = page.source_page_order + 1
  return `Sheet ${sheet} not aligned: readings may be shifted. Check each one against the full sheet.`
}

const TYPE_NAME: Record<SubmissionResultType, string> = {
  QUESTION: 'Question',
  MATRIX: 'Matrix',
  FREE_TEXT: 'Free text',
  CODE: 'Code',
  RESPONDENT_IDENTIFIER: 'Identifier',
}

/** The object's authored label, or its type when it has none. */
export function objectTitle(obj: Pick<SubmissionReviewObject, 'label' | 'result_type'>): string {
  const label = obj.label?.trim()
  return label || (TYPE_NAME[obj.result_type] ?? obj.result_type)
}

/** The one-line hint under the title: the question's text, or what the OCR
    read; "" when there is nothing worth a line. */
export function objectSubtitle(
  obj: Pick<SubmissionReviewObject, 'question_text' | 'result_type' | 'detected_payload'>,
): string {
  if (obj.question_text) return obj.question_text
  if (obj.result_type === 'FREE_TEXT' && isRecord(obj.detected_payload)) {
    const text = obj.detected_payload.suggested_text
    if (typeof text === 'string' && text.trim() !== '') return `OCR: “${text}”`
  }
  return ''
}

// ── Selection ────────────────────────────────────────────────────────────────

/**
 * The reading selected when the page opens: the first unresolved one in page
 * order (where the work is), otherwise the first one (a fully resolved
 * submission still shows something), otherwise null (nothing to review).
 */
export function defaultSelection(detail: Pick<SubmissionReviewDetail, 'pages'>): string | null {
  const all = detail.pages.flatMap(p => p.objects)
  return (all.find(o => !o.resolved) ?? all[0])?.result_id ?? null
}

// ── Lock ─────────────────────────────────────────────────────────────────────

/** Where the page's own acquire stands: not answered yet, or answered with
    whether the lock is ours. */
export type AcquireState =
  | { settled: false }
  | { settled: true; acquired: boolean }

export type LockView = 'closed' | 'pending' | 'mine' | 'held' | 'lost'

/**
 * Which of the page's lock situations applies, in priority order:
 *
 *  - `closed`  — not SCANNED: read-only, and no lock was ever requested;
 *  - `pending` — our acquire has not answered yet;
 *  - `mine`    — we acquired it and the review shows no lock or ours;
 *  - `lost`    — we had it at some point, and the review now shows a
 *                colleague's active lock: they took it over;
 *  - `held`    — we never got it: a colleague was reviewing when we opened.
 *
 * `detailLock` is the review payload's (active-only) lock, refreshed on focus
 * and Refresh — which is how a take-over by a colleague shows up here.
 */
export function lockView({
  status, acquire, detailLock, currentUserId, everAcquired,
}: {
  status: SubmissionStatus
  acquire: AcquireState
  detailLock: Pick<SubmissionLockInfo, 'user_id'> | null
  currentUserId: string | undefined
  everAcquired: boolean
}): LockView {
  if (status !== 'SCANNED') return 'closed'
  if (!acquire.settled) return 'pending'
  // With the profile not loaded we cannot tell whose the lock is; never claim
  // a colleague took it on that basis.
  const heldByOther =
    detailLock !== null && currentUserId !== undefined && detailLock.user_id !== currentUserId
  if (acquire.acquired && !heldByOther) return 'mine'
  // Acquired implies everAcquired in the page; both are checked so the rule
  // reads as stated: we had it, a colleague has it now.
  if ((everAcquired || acquire.acquired) && heldByOther) return 'lost'
  return 'held'
}

// ── Scan crop ────────────────────────────────────────────────────────────────
// The image served is the one the extractors read on: for a sheet aligned, its
// straightened copy, already in the template's frame and the right way up; for
// a sheet not aligned or never aligned, the original scan. The extractors map
// template geometry onto it with independent per-axis scale factors, so a
// percentage of the template page IS the same percentage of the image.
// Stretching the whole image to the template's aspect ratio therefore shows
// exactly the pixels the extractor sampled: on an aligned sheet exactly the
// answer; on a sheet not aligned the crop is off exactly as the reading was,
// and the operator can see it.

/** Context kept around the object in the crop, in template-page pixels. */
export const CROP_MARGIN_PX = 24

export interface ScanFrame {
  /** Width / height of the visible frame. */
  aspectRatio: number
  /** The whole scan's box, as percentages of the frame (may overflow it). */
  image: PercentRect
  /** The object's box, as percentages of the frame. Null when the object has
      no area to draw. */
  highlight: PercentRect | null
}

type TemplateSize = Pick<SubmissionReviewTemplatePage, 'width_px' | 'height_px'>

/** The template page's pixel space; the A4 fallback for each dimension that is
    null, missing or not positive (a zero would make every percentage infinite). */
function templateSize(page: TemplateSize | null): PageSize {
  const w = page?.width_px
  const h = page?.height_px
  return {
    width: w != null && w > 0 ? w : FALLBACK_PAGE_WIDTH,
    height: h != null && h > 0 ? h : FALLBACK_PAGE_HEIGHT,
  }
}

const WHOLE_IMAGE: PercentRect = { leftPct: 0, topPct: 0, widthPct: 100, heightPct: 100 }

/**
 * Where to place the scan so the object, grown by `marginPx` on every side and
 * clamped to the page, fills the frame. Degenerate input — an object with no
 * area, or one wholly outside the page — gives the whole page, with no
 * highlight, rather than dividing by zero.
 */
export function cropFrame(
  geometry: SubmissionReviewGeometry,
  templatePage: TemplateSize | null,
  marginPx = CROP_MARGIN_PX,
): ScanFrame {
  const page = templateSize(templatePage)
  const left = Math.max(0, geometry.x - marginPx)
  const top = Math.max(0, geometry.y - marginPx)
  const right = Math.min(page.width, geometry.x + geometry.width + marginPx)
  const bottom = Math.min(page.height, geometry.y + geometry.height + marginPx)
  const expW = right - left
  const expH = bottom - top
  if (geometry.width <= 0 || geometry.height <= 0 || expW <= 0 || expH <= 0) {
    return { aspectRatio: page.width / page.height, image: WHOLE_IMAGE, highlight: null }
  }
  return {
    aspectRatio: expW / expH,
    image: {
      widthPct: (page.width / expW) * 100,
      heightPct: (page.height / expH) * 100,
      leftPct: (-left / expW) * 100,
      topPct: (-top / expH) * 100,
    },
    highlight: {
      leftPct: ((geometry.x - left) / expW) * 100,
      topPct: ((geometry.y - top) / expH) * 100,
      widthPct: (geometry.width / expW) * 100,
      heightPct: (geometry.height / expH) * 100,
    },
  }
}

/** The whole sheet at the template's aspect, with the object's box on it. */
export function fullSheetFrame(
  geometry: SubmissionReviewGeometry,
  templatePage: TemplateSize | null,
): ScanFrame {
  const page = templateSize(templatePage)
  return {
    aspectRatio: page.width / page.height,
    image: WHOLE_IMAGE,
    highlight: geometry.width > 0 && geometry.height > 0 ? toPercentRect(geometry, page) : null,
  }
}

// ── Review actions ───────────────────────────────────────────────────────────
// Which editor a reading is replaced with, what it starts from, what it sends,
// the live hints under it, and how a ruling reads afterwards. Whether a reading
// may be confirmed or bulk-accepted is NOT here: the BE sends `confirmable` and
// `bulk_acceptable`, and the FE never re-derives them.

export type EditorKind = 'question' | 'matrix' | 'identifier' | 'freeText' | 'code'

type ActionObject = Pick<
  SubmissionReviewObject,
  'result_type' | 'role' | 'detected_payload' | 'review_outcome' | 'reviewed_payload'
>

/**
 * The replacement editor for `obj`. An identity is an identity whatever was
 * read: a respondent-ID grid (RESPONDENT_IDENTIFIER) and a FREE_TEXT_AREA holding
 * the RESPONDENT_IDENTIFIER role both take a single value, as the BE expects.
 */
export function editorKind(obj: Pick<SubmissionReviewObject, 'result_type' | 'role'>): EditorKind {
  if (obj.result_type === 'RESPONDENT_IDENTIFIER' || obj.role === 'RESPONDENT_IDENTIFIER') {
    return 'identifier'
  }
  switch (obj.result_type) {
    case 'QUESTION': return 'question'
    case 'MATRIX': return 'matrix'
    case 'FREE_TEXT': return 'freeText'
    default: return 'code'
  }
}

/**
 * Why a ruling on `obj` will not change the candidate, or null when
 * there is nothing to say: not an identity, or the first identity reading of
 * the submission (`sets_candidate`, the server's flag). `candidatePage` is the
 * sheet of that first reading (`candidate_reading_page_id`), null if unknown;
 * `firstListed` whether that reading is in the review list (some listed
 * object has `sets_candidate`). When it is not — it needed no review — the
 * operator cannot open it, so the note points to the header.
 */
export function candidateNote(
  obj: Pick<SubmissionReviewObject, 'result_type' | 'role' | 'sets_candidate'>,
  candidatePage: Pick<SubmissionReviewPage, 'source_page_order' | 'template_page'> | null,
  firstListed: boolean,
): string | null {
  if (editorKind(obj) !== 'identifier' || obj.sets_candidate) return null
  if (!candidatePage) return 'Does not set the candidate: only the first reading in page order does.'
  const sheet = candidatePage.source_page_order + 1
  const page = candidatePage.template_page ? ` (page ${candidatePage.template_page.page_index})` : ''
  return firstListed
    ? `Does not set the candidate: the reading on sheet ${sheet}${page} does.`
    : `Does not set the candidate: the reading on sheet ${sheet}${page} does, and is not in review — edit the candidate in the header.`
}

/** What an editor holds. One shape for every kind: each kind reads only its
    own field (`selectedOptionIds`, `cells` as "row:col", or `text`). */
export interface EditorState {
  selectedOptionIds: string[]
  cells: Set<string>
  text: string
}

export function cellKey(row: number, col: number): string {
  return `${row}:${col}`
}

function numbers(value: unknown): number[] {
  return Array.isArray(value) ? value.filter((n): n is number => typeof n === 'number') : []
}

/** The option ids the EXTRACTOR read as marked — highlighted "read" in the
    question editor whatever the operator then ticks. */
export function detectedOptionIds(obj: Pick<SubmissionReviewObject, 'detected_payload'>): string[] {
  const payload = obj.detected_payload
  if (!isRecord(payload)) return []
  return (records(payload.selected_options) ?? [])
    .map(o => o.option_id)
    .filter((id): id is string => typeof id === 'string')
}

/** The cells ("row:col") the EXTRACTOR read as marked, from either grouping. */
export function detectedCells(obj: Pick<SubmissionReviewObject, 'detected_payload'>): Set<string> {
  const payload = obj.detected_payload
  const cells = new Set<string>()
  if (!isRecord(payload)) return cells
  for (const row of records(payload.rows) ?? []) {
    for (const c of records(row.selected_cells) ?? []) {
      cells.add(cellKey(Number(row.row_index), Number(c.col_index)))
    }
  }
  for (const col of records(payload.cols) ?? []) {
    for (const c of records(col.selected_cells) ?? []) {
      cells.add(cellKey(Number(c.row_index), Number(col.col_index)))
    }
  }
  return cells
}

function reviewedCells(payload: Record<string, unknown>): Set<string> {
  const cells = new Set<string>()
  for (const row of records(payload.rows) ?? []) {
    for (const c of numbers(row.selected_col_indices)) cells.add(cellKey(Number(row.row_index), c))
  }
  for (const col of records(payload.cols) ?? []) {
    for (const r of numbers(col.selected_row_indices)) cells.add(cellKey(r, Number(col.col_index)))
  }
  return cells
}

function stringField(payload: unknown, field: string): string {
  return isRecord(payload) && typeof payload[field] === 'string' ? (payload[field] as string) : ''
}

/**
 * Where the replacement editor starts: the value already entered when the
 * reading is SUBSTITUTED (so a correction is corrected, not retyped), the
 * extractor's reading otherwise — ``?`` digits and all, so the operator sees
 * the gap. With `config`, an option or cell that no longer exists in the
 * authored object is dropped, so the editor never sends what the BE refuses.
 */
export function initialEditorState(obj: ActionObject, config?: PageObjectDetail): EditorState {
  const kind = editorKind(obj)
  const reviewed =
    obj.review_outcome === 'SUBSTITUTED' && isRecord(obj.reviewed_payload)
      ? (obj.reviewed_payload as Record<string, unknown>)
      : null
  const state: EditorState = { selectedOptionIds: [], cells: new Set(), text: '' }
  switch (kind) {
    case 'question': {
      const ids = reviewed
        ? (Array.isArray(reviewed.selected_option_ids)
            ? reviewed.selected_option_ids.filter((i): i is string => typeof i === 'string')
            : [])
        : detectedOptionIds(obj)
      const authored = config?.question_config?.options
      const known = authored ? new Set(authored.map(o => o.id)) : null
      state.selectedOptionIds = [...new Set(ids)].filter(id => !known || known.has(id))
      break
    }
    case 'matrix': {
      const cells = reviewed ? reviewedCells(reviewed) : detectedCells(obj)
      const m = config?.matrix_config
      state.cells = m
        ? new Set([...cells].filter(k => {
            const [r, c] = k.split(':').map(Number)
            return r >= 0 && r < m.rows_count && c >= 0 && c < m.cols_count
          }))
        : cells
      break
    }
    case 'identifier':
      state.text = reviewed
        ? stringField(reviewed, 'value')
        : obj.result_type === 'RESPONDENT_IDENTIFIER'
          ? stringField(obj.detected_payload, 'raw_value')
          : stringField(obj.detected_payload, 'suggested_text')
      break
    case 'freeText':
      state.text = reviewed ? stringField(reviewed, 'text') : stringField(obj.detected_payload, 'suggested_text')
      break
    case 'code': {
      const decoded = isRecord(obj.detected_payload) ? obj.detected_payload.decoded_value : null
      state.text = reviewed ? stringField(reviewed, 'value') : typeof decoded === 'string' ? decoded : ''
      break
    }
  }
  return state
}

/** The SUBSTITUTE `value` for what the editor holds. Text is trimmed as the BE
    trims it; cells are sent in row, then column order. */
export function buildSubstituteValue(kind: EditorKind, state: EditorState): SubstituteValue {
  switch (kind) {
    case 'question':
      return { selected_option_ids: [...state.selectedOptionIds] }
    case 'matrix':
      return {
        selected_cells: [...state.cells]
          .map(k => k.split(':').map(Number) as [number, number])
          .sort((a, b) => a[0] - b[0] || a[1] - b[1]),
      }
    case 'identifier':
      return { value: state.text.trim() }
    case 'freeText':
      return { text: state.text.trim() }
    case 'code':
      return { value: state.text.trim() }
  }
}

/** A live hint under an editor. `warn` when the server will NOT read the
    entry as a plain valid answer. Informational only: the server decides. */
export interface EditorHint {
  text: string
  warn: boolean
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`
}

/** What the ticked options will count as. */
export function questionHint(questionType: QuestionType, count: number): EditorHint {
  if (count === 0) return { text: 'Counts as no answer', warn: true }
  if (questionType === 'SINGLE_CHOICE' && count > 1) {
    return {
      text: 'Counts as an invalid answer (more than one option on a single-choice question)',
      warn: true,
    }
  }
  return { text: `${plural(count, 'option', 'options')} selected`, warn: false }
}

const ROW_GROUPED: readonly MatrixSelectionRule[] = ['ONE_PER_ROW', 'MANY_PER_ROW', 'FREE']
const SINGLE_SELECTION: readonly MatrixSelectionRule[] = ['ONE_PER_ROW', 'ONE_PER_COLUMN']

/**
 * The groups (rows under ONE_PER_ROW / MANY_PER_ROW / FREE, columns otherwise —
 * the BE's grouping) left with no mark and, under a single-selection rule,
 * with more than one.
 */
export function matrixHint(
  rule: MatrixSelectionRule,
  cells: ReadonlySet<string>,
  rows: number,
  cols: number,
): EditorHint {
  const byRow = ROW_GROUPED.includes(rule)
  const groups = byRow ? rows : cols
  const counts = new Array<number>(Math.max(groups, 0)).fill(0)
  for (const k of cells) {
    const [r, c] = k.split(':').map(Number)
    const g = byRow ? r : c
    if (g >= 0 && g < groups) counts[g] += 1
  }
  const empty = counts.filter(n => n === 0).length
  const tooMany = SINGLE_SELECTION.includes(rule) ? counts.filter(n => n > 1).length : 0
  const [one, many] = byRow ? ['row', 'rows'] : ['column', 'columns']
  const parts: string[] = []
  if (empty > 0) parts.push(`${plural(empty, one, many)} with no mark`)
  if (tooMany > 0) parts.push(`${plural(tooMany, one, many)} with more than one mark`)
  if (parts.length > 0) return { text: parts.join(', '), warn: true }
  return {
    text: SINGLE_SELECTION.includes(rule)
      ? `All ${many} have one mark`
      : `All ${many} have at least one mark`,
    warn: false,
  }
}

function reviewedQuestionText(
  payload: Record<string, unknown>,
  config: PageObjectDetail | undefined,
): string | null {
  if (!Array.isArray(payload.selected_option_ids)) return null
  const ids = payload.selected_option_ids.map(String)
  if (ids.length === 0) return 'no option marked'
  const options = config?.question_config?.options
  if (!options) return ids.length === 1 ? '1 option' : `${ids.length} options`
  const position = new Map(
    [...options].sort((a, b) => a.order_index - b.order_index).map((o, i) => [o.id, i]),
  )
  return ids
    .map(id => position.get(id))
    .sort((a, b) => (a ?? Infinity) - (b ?? Infinity))
    .map(i => (i === undefined ? '?' : optionLetter(i)))
    .join(', ')
}

function reviewedMatrixText(
  payload: Record<string, unknown>,
  config: PageObjectDetail | undefined,
): string | null {
  const rows = records(payload.rows) ?? []
  const cols = records(payload.cols) ?? []
  const byRow = rows.length > 0
  const groups = byRow ? rows : cols
  if (groups.length === 0) return null
  const matrix = config?.matrix_config
  const rowLabel = new Map((matrix?.rows ?? []).map(r => [r.order_index, r.label]))
  const colLabel = new Map((matrix?.cols ?? []).map(c => [c.order_index, c.label]))
  const nameRow = (i: number) => rowLabel.get(i) ?? `Row ${i + 1}`
  const nameCol = (i: number) => colLabel.get(i) ?? `Column ${i + 1}`
  const cellRow = (i: number) => rowLabel.get(i) ?? String(i + 1)
  const cellCol = (i: number) => colLabel.get(i) ?? String(i + 1)
  return groups
    .map(group => {
      const name = byRow ? nameRow(Number(group.row_index)) : nameCol(Number(group.col_index))
      const marks = byRow
        ? numbers(group.selected_col_indices).map(cellCol)
        : numbers(group.selected_row_indices).map(cellRow)
      return `${name}: ${marks.length === 0 ? 'no mark' : marks.join(', ')}`
    })
    .join('; ')
}

/**
 * "Replaced with: …" for a SUBSTITUTED reading, in the style of
 * formatProposedReading: option letters, matrix labels (indices without the
 * config), the value as entered, free text quoted ("nothing written" when the
 * operator left it empty). An unexpected payload gives plain "Replaced".
 */
export function formatReviewedValue(
  obj: Pick<SubmissionReviewObject, 'result_type' | 'role' | 'reviewed_payload'>,
  config?: PageObjectDetail,
): string {
  const payload = obj.reviewed_payload
  if (!isRecord(payload)) return 'Replaced'
  let text: string | null
  switch (editorKind(obj)) {
    case 'question': text = reviewedQuestionText(payload, config); break
    case 'matrix': text = reviewedMatrixText(payload, config); break
    case 'freeText': {
      const t = stringField(payload, 'text')
      text = t === '' ? 'nothing written' : `“${t}”`
      break
    }
    default: {
      const v = stringField(payload, 'value')
      text = v === '' ? null : v
    }
  }
  return text === null ? 'Replaced' : `Replaced with: ${text}`
}

const OUTCOME_LABEL: Record<ReviewOutcome, string> = {
  CONFIRMED: 'Confirmed',
  SUBSTITUTED: 'Replaced',
  FLAGGED: 'Flagged as unresolvable',
}
const OUTCOME_ICON: Record<ReviewOutcome, string> = {
  CONFIRMED: '✓',
  SUBSTITUTED: '✎',
  FLAGGED: '⚑',
}

export function outcomeLabel(outcome: ReviewOutcome): string {
  return OUTCOME_LABEL[outcome] ?? outcome
}

export function outcomeIcon(outcome: ReviewOutcome): string {
  return OUTCOME_ICON[outcome] ?? '✓'
}

/** When a ruling was made, in local time: the time alone today, the date and
    time otherwise (a ruling from yesterday must not read as this morning). */
export function formatReviewedAt(iso: string, now: Date = new Date()): string {
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return ''
  const time = at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  return at.toDateString() === now.toDateString()
    ? time
    : `${at.toLocaleDateString()} ${time}`
}

/**
 * The reading to select after a ruling: the first unresolved one AFTER
 * `currentResultId` in list order (sheet by sheet, as listed), wrapping round
 * to the start; never the current one itself. With the current one not found,
 * the first unresolved. Null when nothing else is unresolved.
 */
export function nextUnresolvedResultId(
  detail: Pick<SubmissionReviewDetail, 'pages'>,
  currentResultId: string | null,
): string | null {
  const all = detail.pages.flatMap(p => p.objects)
  const start = all.findIndex(o => o.result_id === currentResultId)
  for (let step = 1; step <= all.length; step++) {
    const candidate = all[(start + step) % all.length]
    if (candidate.result_id !== currentResultId && !candidate.resolved) return candidate.result_id
  }
  return null
}

// ── Keyboard flow ────────────────────────────────────────────────────────────

/**
 * The reading before / after `currentResultId` in list order (sheet by sheet,
 * as listed), for ↑ / ↓. No wrap: null at either end, and when the current
 * one is not in the list.
 */
export function adjacentResultId(
  detail: Pick<SubmissionReviewDetail, 'pages'>,
  currentResultId: string | null,
  direction: 'next' | 'prev',
): string | null {
  const all = detail.pages.flatMap(p => p.objects)
  const index = all.findIndex(o => o.result_id === currentResultId)
  if (index < 0) return null
  return all[index + (direction === 'next' ? 1 : -1)]?.result_id ?? null
}

export type ReviewKeyAction = 'confirm' | 'replace' | 'flag' | 'undo' | 'next' | 'prev' | 'escape'

export interface ReviewKeyEvent {
  key: string
  ctrlKey: boolean
  metaKey: boolean
  altKey: boolean
  repeat: boolean
  /** Already handled where it happened (the editor, the note): not the page's. */
  defaultPrevented: boolean
  /** The event comes from an input, textarea, select or contenteditable:
      those keys belong to the field (the editors handle their own). */
  targetIsField: boolean
  /** The event comes from a button or link that Enter activates natively
      (other than a list row): Enter is left to it, or it would act twice. */
  targetIsControl: boolean
}

export interface ReviewKeyContext {
  lockMine: boolean
  scanned: boolean
  /** The Replace editor is open. */
  editorOpen: boolean
  /** The note textarea is open. */
  noteOpen: boolean
  /** A confirmation dialog (or any modal) is open. */
  dialogOpen: boolean
  /** An action or "Accept all remaining" is in flight. */
  pending: boolean
  selected: { resolved: boolean; confirmable: boolean } | null
}

/**
 * What a key pressed on the review page does — THE keyboard map, so the page's
 * listener only dispatches:
 *
 *   Enter  confirm (lock ours, SCANNED, selection unresolved and confirmable)
 *   R      replace (lock ours, SCANNED)
 *   F      flag    (lock ours, SCANNED)
 *   U      undo    (lock ours, SCANNED, selection resolved)
 *   ↓ / J  next object, ↑ / K previous — read-only views too
 *   Escape close the open editor or note, sending nothing
 *
 * The four writing keys and the arrows need no editor open (an arrow would
 * select away from the reading being replaced and lose the edit). Nothing
 * happens with Ctrl / ⌘ / Alt held, with a dialog open, while a save is in
 * flight, from inside a field, or for a key the editor or the note already
 * handled; a held-down Enter / F / U does not repeat,
 * so a held key never fires several saves.
 */
export function reviewKeyAction(event: ReviewKeyEvent, ctx: ReviewKeyContext): ReviewKeyAction | null {
  if (event.ctrlKey || event.metaKey || event.altKey) return null
  if (event.defaultPrevented || ctx.dialogOpen || ctx.pending || event.targetIsField) return null
  const key = event.key.length === 1 ? event.key.toLowerCase() : event.key
  const canWrite = ctx.lockMine && ctx.scanned && !ctx.editorOpen && ctx.selected !== null
  switch (key) {
    case 'Enter':
      if (event.repeat || event.targetIsControl) return null
      return canWrite && !ctx.selected!.resolved && ctx.selected!.confirmable ? 'confirm' : null
    case 'r':
      return canWrite ? 'replace' : null
    case 'f':
      return canWrite && !event.repeat ? 'flag' : null
    case 'u':
      return canWrite && !event.repeat && ctx.selected!.resolved ? 'undo' : null
    case 'ArrowDown':
    case 'j':
      return ctx.editorOpen ? null : 'next'
    case 'ArrowUp':
    case 'k':
      return ctx.editorOpen ? null : 'prev'
    case 'Escape':
      return ctx.editorOpen || ctx.noteOpen ? 'escape' : null
    default:
      return null
  }
}

// ── Closing the submission ───────────────────────────────────────────────────

/** One thing worth seeing before closing: what it is, then what it means. */
export interface CloseWarning {
  title: string
  detail: string
}

type Closable = Pick<
  SubmissionReviewDetail,
  'pending_review_count' | 'respondent_identifier' | 'respondent_identifier_expected' | 'missing_page_indices'
>

/**
 * What the "Close submission" confirmation lists: readings
 * still unresolved, no candidate where the variant expects one (the header's
 * "No candidate" rule), template pages no sheet was bound to. None of them
 * blocks the close; an EMPTY list means the click closes with no dialog.
 */
export function closeWarnings(detail: Closable): CloseWarning[] {
  const warnings: CloseWarning[] = []
  const n = detail.pending_review_count
  if (n > 0) {
    warnings.push(
      n === 1
        ? {
            title: '1 object is still unresolved.',
            detail: 'It will count as no answer (0 points) and as not reviewed in the export.',
          }
        : {
            title: `${n} objects are still unresolved.`,
            detail: 'They will count as no answer (0 points) and as not reviewed in the export.',
          },
    )
  }
  if (showNoCandidate(detail)) {
    warnings.push({ title: 'No candidate.', detail: 'The submission is not attributed to anyone.' })
  }
  const missing = detail.missing_page_indices
  if (missing.length > 0) {
    warnings.push(
      missing.length === 1
        ? { title: `Missing page: ${missing[0]}.`, detail: 'Its objects have no reading.' }
        : { title: `Missing pages: ${missing.join(', ')}.`, detail: 'Their objects have no reading.' },
    )
  }
  return warnings
}

/**
 * The header's review count: "To review — 2 of 6 remaining" while open, and
 * on a closed submission "Unresolved — 2 of 6 at closing": the
 * same count, which on a closed submission is not work to do but what was left.
 */
export function reviewCountLine(
  detail: Pick<SubmissionReviewDetail, 'status' | 'pending_review_count' | 'review_count'>,
): { label: string; value: string } {
  const { pending_review_count: pending, review_count: total } = detail
  return isClosedStatus(detail.status)
    ? { label: 'Unresolved', value: `${pending} of ${total} at closing` }
    : { label: 'To review', value: `${pending} of ${total} remaining` }
}

// ── Reopening the submission ─────────────────────────────────────────────────

/**
 * Whether "Reopen submission" is offered: on a REVIEWED
 * submission only. FINALIZED is closed too, but is not reopened from this
 * page, so no button for it; UNRESOLVABLE has its own Undo.
 */
export function canReopen(status: SubmissionStatus): boolean {
  return status === 'REVIEWED'
}
