// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  adjacentResultId,
  buildSubstituteValue,
  canReopen,
  candidateNote,
  cellKey,
  closeWarnings,
  cropFrame,
  defaultSelection,
  detectedCells,
  detectedOptionIds,
  editorKind,
  formatProposedReading,
  formatReviewedAt,
  formatReviewedValue,
  fullSheetFrame,
  initialEditorState,
  lockView,
  matrixHint,
  nextUnresolvedResultId,
  outcomeIcon,
  outcomeLabel,
  questionHint,
  reviewCountLine,
  reviewKeyAction,
  type ReviewKeyContext,
  type ReviewKeyEvent,
  objectSubtitle,
  objectTitle,
  optionLetter,
  pageAlignmentWarning,
  pageGroupTitle,
  reasonChipClass,
  reasonExplanation,
  reasonLabel,
  type AcquireState,
} from './submissionReview.ts'
import type {
  PageObjectDetail,
  SubmissionResultType,
  SubmissionReviewObject,
  SubmissionReviewPage,
  SubmissionStatus,
} from '../api/domain.ts'

function obj(
  resultId: string,
  extra: Partial<SubmissionReviewObject> = {},
): SubmissionReviewObject {
  return {
    result_id: resultId,
    page_object_id: `po-${resultId}`,
    object_type: 'QUESTION',
    result_type: 'QUESTION',
    role: null,
    label: null,
    question_text: null,
    geometry: { x: 0, y: 0, width: 10, height: 10 },
    detected_payload: null,
    confidence_score: null,
    flag_reasons: [],
    resolved: false,
    review_outcome: null,
    reviewed_payload: null,
    review_note: null,
    reviewed_by: null,
    reviewed_at: null,
    confirmable: true,
    bulk_acceptable: true,
    sets_candidate: false,
    ...extra,
  }
}

function page(
  order: number,
  pageIndex: number | null,
  objects: SubmissionReviewObject[] = [],
): SubmissionReviewPage {
  return {
    id: `page-${order}`,
    source_page_order: order,
    page_order_source: 'CODE',
    alignment_status: 'ALIGNED',
    template_page: pageIndex === null
      ? null
      : { id: `tp-${pageIndex}`, page_index: pageIndex, width_px: 794, height_px: 1123 },
    objects,
  }
}

function reading(resultType: SubmissionResultType, payload: unknown, config?: PageObjectDetail) {
  return formatProposedReading(
    { result_type: resultType, detected_payload: payload as Record<string, unknown> | null },
    config,
  )
}

// ── Reasons ──────────────────────────────────────────────────────────────────

const KNOWN: [string, string, string][] = [
  ['LOW_CONFIDENCE', 'Low confidence', 'nd-chip-warning'],
  ['INVALID_ANSWER', 'Invalid answer', 'nd-chip-danger'],
  ['ALWAYS_REVIEW', 'Always reviewed', 'nd-chip-info'],
  ['DECODE_FAILED', 'Code not decoded', 'nd-chip-danger'],
  ['AMBIGUOUS_DIGITS', 'Ambiguous digits', 'nd-chip-danger'],
  ['MISSING_DIGITS', 'Missing digits', 'nd-chip-danger'],
  ['UNREADABLE_GRID', 'Grid not readable', 'nd-chip-danger'],
  ['EXTRACTION_FAILED', 'Could not be read', 'nd-chip-danger'],
  ['SHEET_NOT_ALIGNED', 'Sheet not aligned', 'nd-chip-warning'],
]

test('every known reason has its label and chip colour', () => {
  for (const [code, label, cls] of KNOWN) {
    assert.equal(reasonLabel(code), label)
    assert.equal(reasonChipClass(code), cls)
  }
})

test('an unknown reason shows its raw code, muted', () => {
  assert.equal(reasonLabel('SOMETHING_NEW'), 'SOMETHING_NEW')
  assert.equal(reasonChipClass('SOMETHING_NEW'), 'nd-chip-muted')
})

test('low confidence names the batch threshold as a percent', () => {
  assert.equal(
    reasonExplanation('LOW_CONFIDENCE', 0.8),
    'Read with low confidence (below the 80% threshold).',
  )
  assert.equal(
    reasonExplanation('LOW_CONFIDENCE', 0.825),
    'Read with low confidence (below the 82.5% threshold).',
  )
})

test('every other known reason has one explanatory sentence', () => {
  assert.equal(
    reasonExplanation('INVALID_ANSWER', 0.8),
    'The marks do not respect the question’s rules, e.g. two answers on a single-choice question.',
  )
  assert.equal(reasonExplanation('ALWAYS_REVIEW', 0.8), 'Handwritten text is always checked by a person.')
  assert.equal(reasonExplanation('DECODE_FAILED', 0.8), 'The printed code could not be decoded.')
  assert.equal(
    reasonExplanation('AMBIGUOUS_DIGITS', 0.8),
    'More than one digit is marked in at least one column.',
  )
  assert.equal(reasonExplanation('MISSING_DIGITS', 0.8), 'At least one column has no digit marked.')
  assert.equal(reasonExplanation('UNREADABLE_GRID', 0.8), 'The identifier grid could not be read.')
  assert.equal(
    reasonExplanation('EXTRACTION_FAILED', 0.8),
    'This answer could not be read from the scan. Enter it from the paper sheet.',
  )
})

test('a sheet not aligned explains the shift and points to the full sheet', () => {
  assert.equal(
    reasonExplanation('SHEET_NOT_ALIGNED', 0.8),
    'This sheet could not be aligned to the template, so the reading may be shifted. Check it against the full sheet.',
  )
})

test('an unknown reason has no explanation', () => {
  assert.equal(reasonExplanation('SOMETHING_NEW', 0.8), '')
})

// ── optionLetter ─────────────────────────────────────────────────────────────

test('option letters are A, B, C … from 0', () => {
  assert.equal(optionLetter(0), 'A')
  assert.equal(optionLetter(1), 'B')
  assert.equal(optionLetter(25), 'Z')
  assert.equal(optionLetter(26), 'AA')
  assert.equal(optionLetter(27), 'AB')
})

// ── formatProposedReading ────────────────────────────────────────────────────

const QUESTION_CONFIG = {
  question_config: {
    options: [
      // Deliberately out of order: letters follow order_index, not the array.
      { id: 'opt-c', order_index: 2 },
      { id: 'opt-a', order_index: 0 },
      { id: 'opt-b', order_index: 1 },
    ],
  },
} as unknown as PageObjectDetail

test('a question reads as option letters with their confidence, in bubble order', () => {
  const payload = {
    selected_options: [
      { option_id: 'opt-c', confidence: 0.41 },
      { option_id: 'opt-b', confidence: 0.96 },
    ],
    validity: 'TOO_MANY_ANSWERS',
  }
  assert.deepEqual(reading('QUESTION', payload, QUESTION_CONFIG), ['B (96%), C (41%)'])
})

test('a question option no longer authored reads as "?", last', () => {
  const payload = {
    selected_options: [
      { option_id: 'gone', confidence: 0.9 },
      { option_id: 'opt-a', confidence: 0.8 },
    ],
  }
  assert.deepEqual(reading('QUESTION', payload, QUESTION_CONFIG), ['A (80%), ? (90%)'])
})

test('a question with nothing marked says so', () => {
  assert.deepEqual(
    reading('QUESTION', { selected_options: [], validity: 'NO_ANSWER' }, QUESTION_CONFIG),
    ['No option marked'],
  )
})

test('a question without its config shows the confidences only', () => {
  assert.deepEqual(
    reading('QUESTION', { selected_options: [{ option_id: 'x', confidence: 0.96 }] }),
    ['1 option marked (96%)'],
  )
  assert.deepEqual(
    reading('QUESTION', {
      selected_options: [{ option_id: 'x', confidence: 0.96 }, { option_id: 'y', confidence: 0.41 }],
    }),
    ['2 options marked (96%, 41%)'],
  )
  assert.deepEqual(
    reading('QUESTION', { selected_options: [{ option_id: 'x' }] }),
    ['1 option marked'],
  )
})

const MATRIX_CONFIG = {
  matrix_config: {
    rows: [{ order_index: 0, label: 'Sodium' }, { order_index: 1, label: 'Chlorine' }],
    cols: [{ order_index: 0, label: 'Metal' }, { order_index: 1, label: 'Non-metal' }],
  },
} as unknown as PageObjectDetail

test('a row-oriented matrix reads one line per row, with labels', () => {
  const payload = {
    rows: [
      { row_index: 0, selected_cells: [{ col_index: 0, confidence: 0.9 }], validity: 'VALID' },
      { row_index: 1, selected_cells: [], validity: 'NO_ANSWER' },
    ],
    cols: [],
  }
  assert.deepEqual(reading('MATRIX', payload, MATRIX_CONFIG), [
    'Sodium: Metal (90%)',
    'Chlorine: no mark',
  ])
})

test('a column-oriented matrix reads one line per column', () => {
  const payload = {
    rows: [],
    cols: [
      {
        col_index: 1,
        selected_cells: [{ row_index: 0, confidence: 0.5 }, { row_index: 1, confidence: 0.99 }],
        validity: 'TOO_MANY_ANSWERS',
      },
    ],
  }
  assert.deepEqual(reading('MATRIX', payload, MATRIX_CONFIG), ['Non-metal: Sodium (50%), Chlorine (99%)'])
})

test('a matrix without its config uses 1-based indices', () => {
  const rows = { rows: [{ row_index: 2, selected_cells: [{ col_index: 3, confidence: 0.7 }] }] }
  assert.deepEqual(reading('MATRIX', rows), ['Row 3: 4 (70%)'])
  const cols = { cols: [{ col_index: 0, selected_cells: [{ row_index: 4, confidence: 0.7 }] }] }
  assert.deepEqual(reading('MATRIX', cols), ['Column 1: 5 (70%)'])
})

test('a matrix with no groups at all has no reading', () => {
  assert.deepEqual(reading('MATRIX', { rows: [], cols: [] }), ['No reading available'])
  assert.deepEqual(reading('MATRIX', { something: 1 }), ['No reading available'])
})

test('an identifier reads as its raw value, ambiguous columns 1-based', () => {
  assert.deepEqual(
    reading('RESPONDENT_IDENTIFIER', { raw_value: '0045871', digits: [], ambiguous_columns: [] }),
    ['0045871'],
  )
  assert.deepEqual(
    reading('RESPONDENT_IDENTIFIER', { raw_value: '00?5871', digits: [], ambiguous_columns: [2, 4] }),
    ['00?5871', 'Ambiguous columns: 3, 5'],
  )
})

test('an unreadable identifier grid has no reading', () => {
  assert.deepEqual(
    reading('RESPONDENT_IDENTIFIER', { raw_value: '', digits: [], ambiguous_columns: [] }),
    ['No reading available'],
  )
})

test('free text is the OCR suggestion in quotes, or nothing legible', () => {
  assert.deepEqual(reading('FREE_TEXT', { suggested_text: 'Mario Rosi' }), ['“Mario Rosi”'])
  assert.deepEqual(reading('FREE_TEXT', { suggested_text: '  ' }), ['Nothing legible'])
})

test('a code is its decoded value, or not decoded', () => {
  assert.deepEqual(reading('CODE', { decoded_value: 'ABC-1', decode_successful: true }), ['ABC-1'])
  assert.deepEqual(reading('CODE', { decoded_value: null, decode_successful: false }), ['Not decoded'])
})

test('a null or unexpected payload has no reading', () => {
  assert.deepEqual(reading('QUESTION', null), ['No reading available'])
  assert.deepEqual(reading('QUESTION', ['not', 'an', 'object']), ['No reading available'])
  assert.deepEqual(reading('QUESTION', { selected_options: 'nope' }), ['No reading available'])
  assert.deepEqual(reading('FREE_TEXT', { suggested_text: 3 }), ['No reading available'])
  assert.deepEqual(reading('CODE', { probe: 'x' }), ['No reading available'])
  assert.deepEqual(reading('RESPONDENT_IDENTIFIER', {}), ['No reading available'])
  assert.deepEqual(reading('UNKNOWN' as SubmissionResultType, { a: 1 }), ['No reading available'])
})

test('a placeholder tells the operator to enter the answer from paper', () => {
  for (const resultType of ['QUESTION', 'MATRIX', 'FREE_TEXT', 'CODE', 'RESPONDENT_IDENTIFIER'] as const) {
    assert.deepEqual(
      formatProposedReading({
        result_type: resultType, detected_payload: null, flag_reasons: ['EXTRACTION_FAILED'],
      }),
      ['No reading: enter it from the paper sheet'],
    )
  }
})

test('a null payload without EXTRACTION_FAILED keeps the generic text', () => {
  assert.deepEqual(
    formatProposedReading({ result_type: 'QUESTION', detected_payload: null, flag_reasons: ['LOW_CONFIDENCE'] }),
    ['No reading available'],
  )
  assert.deepEqual(
    formatProposedReading({ result_type: 'QUESTION', detected_payload: null, flag_reasons: [] }),
    ['No reading available'],
  )
})

test('EXTRACTION_FAILED on a reading that does have a payload shows the payload', () => {
  assert.deepEqual(
    formatProposedReading({
      result_type: 'FREE_TEXT', detected_payload: { suggested_text: 'x' }, flag_reasons: ['EXTRACTION_FAILED'],
    }),
    ['“x”'],
  )
})

// ── Titles ───────────────────────────────────────────────────────────────────

test('a page group is titled by template page then sheet', () => {
  assert.equal(pageGroupTitle(page(2, 4)), 'Page 4 · sheet 3')
  assert.equal(pageGroupTitle(page(0, null)), 'Unplaced sheet 1')
})

test('only a sheet not aligned carries a warning, numbered like its group title', () => {
  assert.equal(
    pageAlignmentWarning({ source_page_order: 1, alignment_status: 'NOT_ALIGNED' }),
    'Sheet 2 not aligned: readings may be shifted. Check each one against the full sheet.',
  )
  assert.equal(pageAlignmentWarning({ source_page_order: 1, alignment_status: 'ALIGNED' }), null)
  // Never aligned: the original is what was read, and no warning.
  assert.equal(pageAlignmentWarning({ source_page_order: 1, alignment_status: null }), null)
  const p = { ...page(1, 3), alignment_status: 'NOT_ALIGNED' as const }
  assert.equal(pageGroupTitle(p), 'Page 3 · sheet 2')
  assert.ok(pageAlignmentWarning(p)?.startsWith('Sheet 2 '))
})

test('an object is titled by its label, or its type', () => {
  assert.equal(objectTitle({ label: 'Q5', result_type: 'QUESTION' }), 'Q5')
  assert.equal(objectTitle({ label: '  ', result_type: 'QUESTION' }), 'Question')
  const names: [SubmissionResultType, string][] = [
    ['QUESTION', 'Question'],
    ['MATRIX', 'Matrix'],
    ['FREE_TEXT', 'Free text'],
    ['CODE', 'Code'],
    ['RESPONDENT_IDENTIFIER', 'Identifier'],
  ]
  for (const [type, name] of names) {
    assert.equal(objectTitle({ label: null, result_type: type }), name)
  }
})

test('the subtitle is the question text, the OCR text, or nothing', () => {
  assert.equal(
    objectSubtitle({ question_text: 'The molar mass…', result_type: 'QUESTION', detected_payload: null }),
    'The molar mass…',
  )
  assert.equal(
    objectSubtitle({ question_text: null, result_type: 'FREE_TEXT', detected_payload: { suggested_text: 'Mario' } }),
    'OCR: “Mario”',
  )
  assert.equal(
    objectSubtitle({ question_text: null, result_type: 'FREE_TEXT', detected_payload: { suggested_text: '' } }),
    '',
  )
  assert.equal(
    objectSubtitle({ question_text: null, result_type: 'FREE_TEXT', detected_payload: null }),
    '',
  )
  assert.equal(
    objectSubtitle({ question_text: null, result_type: 'CODE', detected_payload: { decoded_value: 'x' } }),
    '',
  )
})

// ── defaultSelection ─────────────────────────────────────────────────────────

test('the first unresolved reading in page order is selected', () => {
  const detail = {
    pages: [
      page(0, 1, [obj('a', { resolved: true })]),
      page(1, null),
      page(2, 2, [obj('b', { resolved: true }), obj('c'), obj('d')]),
    ],
  }
  assert.equal(defaultSelection(detail), 'c')
})

test('with everything resolved, the first reading is selected', () => {
  const detail = { pages: [page(0, 1, [obj('a', { resolved: true }), obj('b', { resolved: true })])] }
  assert.equal(defaultSelection(detail), 'a')
})

test('with nothing to review, nothing is selected', () => {
  assert.equal(defaultSelection({ pages: [page(0, 1), page(1, null)] }), null)
  assert.equal(defaultSelection({ pages: [] }), null)
})

// ── lockView ─────────────────────────────────────────────────────────────────

const ME = 'me'
const MINE = { user_id: ME }
const LUCA = { user_id: 'luca' }
const ACQUIRED: AcquireState = { settled: true, acquired: true }
const REFUSED: AcquireState = { settled: true, acquired: false }

function view(
  over: Partial<{
    status: SubmissionStatus
    acquire: AcquireState
    detailLock: { user_id: string } | null
    currentUserId: string | undefined
    everAcquired: boolean
  }> = {},
) {
  return lockView({
    status: 'SCANNED',
    acquire: ACQUIRED,
    detailLock: null,
    currentUserId: ME,
    everAcquired: true,
    ...over,
  })
}

test('anything but SCANNED is closed, whatever the lock', () => {
  for (const status of ['REVIEWED', 'FINALIZED', 'MATCHED'] as SubmissionStatus[]) {
    assert.equal(view({ status, detailLock: LUCA, acquire: { settled: false } }), 'closed')
  }
})

test('an acquire not answered yet is pending', () => {
  assert.equal(view({ acquire: { settled: false }, everAcquired: false }), 'pending')
})

test('acquired, with no lock or ours on the review, is mine', () => {
  assert.equal(view({ detailLock: null }), 'mine')
  assert.equal(view({ detailLock: MINE }), 'mine')
})

test('acquired once, a colleague holding it now is lost', () => {
  assert.equal(view({ detailLock: LUCA }), 'lost')
  assert.equal(view({ acquire: REFUSED, detailLock: LUCA, everAcquired: true }), 'lost')
})

test('never acquired is held', () => {
  assert.equal(view({ acquire: REFUSED, detailLock: LUCA, everAcquired: false }), 'held')
  // The colleague left meanwhile: still not ours until we take it.
  assert.equal(view({ acquire: REFUSED, detailLock: null, everAcquired: false }), 'held')
})

test('with the profile not loaded, a lock on the review is never called lost', () => {
  assert.equal(view({ currentUserId: undefined, detailLock: LUCA }), 'mine')
})

// ── Scan crop ────────────────────────────────────────────────────────────────

const PAGE_1000x2000 = { width_px: 1000, height_px: 2000 }

/** Compare every number of a frame with a tolerance: the percentages are
    divisions, and 1/3 * 100 is not exactly representable. */
function close(actual: unknown, expected: unknown, path = 'frame') {
  if (typeof expected === 'number') {
    assert.ok(
      typeof actual === 'number' && Math.abs(actual - expected) < 1e-9,
      `${path}: ${String(actual)} !== ${expected}`,
    )
  } else if (expected === null || typeof expected !== 'object') {
    assert.equal(actual, expected, path)
  } else {
    for (const [key, value] of Object.entries(expected)) {
      close((actual as Record<string, unknown>)[key], value, `${path}.${key}`)
    }
  }
}

test('a crop in the middle of the page grows the object by the margin on every side', () => {
  // Object 100..300 × 500..550 → expanded 76..324 × 476..574 (248 × 98).
  const frame = cropFrame({ x: 100, y: 500, width: 200, height: 50 }, PAGE_1000x2000)
  close(frame, {
    aspectRatio: 248 / 98,
    image: {
      widthPct: (1000 / 248) * 100,
      heightPct: (2000 / 98) * 100,
      leftPct: (-76 / 248) * 100,
      topPct: (-476 / 98) * 100,
    },
    highlight: {
      leftPct: (24 / 248) * 100,
      topPct: (24 / 98) * 100,
      widthPct: (200 / 248) * 100,
      heightPct: (50 / 98) * 100,
    },
  })
})

test('the margin is a parameter', () => {
  const frame = cropFrame({ x: 100, y: 100, width: 100, height: 100 }, PAGE_1000x2000, 0)
  close(frame, {
    aspectRatio: 1,
    image: { widthPct: 1000, heightPct: 2000, leftPct: -100, topPct: -100 },
    highlight: { leftPct: 0, topPct: 0, widthPct: 100, heightPct: 100 },
  })
})

test('a crop touching the top-left corner is clamped to the page', () => {
  // Expanded 0..74 × 0..54: no margin before the object, 24 after it.
  const frame = cropFrame({ x: 0, y: 0, width: 50, height: 30 }, PAGE_1000x2000)
  close(frame, {
    aspectRatio: 74 / 54,
    image: { leftPct: 0, topPct: 0, widthPct: (1000 / 74) * 100, heightPct: (2000 / 54) * 100 },
    highlight: { leftPct: 0, topPct: 0, widthPct: (50 / 74) * 100, heightPct: (30 / 54) * 100 },
  })
})

test('a crop touching the bottom-right corner is clamped to the page', () => {
  // Object 950..1000 × 1970..2000 → expanded 926..1000 × 1946..2000 (74 × 54).
  const frame = cropFrame({ x: 950, y: 1970, width: 50, height: 30 }, PAGE_1000x2000)
  close(frame, {
    aspectRatio: 74 / 54,
    image: { leftPct: (-926 / 74) * 100, topPct: (-1946 / 54) * 100 },
    highlight: {
      leftPct: (24 / 74) * 100,
      topPct: (24 / 54) * 100,
      widthPct: (50 / 74) * 100,
      heightPct: (30 / 54) * 100,
    },
  })
  // The image's right and bottom edges land on the frame's.
  close(frame.image.leftPct + frame.image.widthPct, 100, 'right edge')
  close(frame.image.topPct + frame.image.heightPct, 100, 'bottom edge')
})

test('a template page with no size falls back to A4 at 96 DPI', () => {
  const geometry = { x: 100, y: 100, width: 100, height: 100 }
  const expected = cropFrame(geometry, { width_px: 794, height_px: 1123 })
  close(cropFrame(geometry, { width_px: null, height_px: null }), expected)
  close(cropFrame(geometry, null), expected)
  close(cropFrame(geometry, { width_px: 0, height_px: 0 }), expected)
  close(fullSheetFrame(geometry, null), { aspectRatio: 794 / 1123 })
})

test('degenerate geometry shows the whole page, with nothing highlighted', () => {
  const whole = {
    aspectRatio: 0.5,
    image: { leftPct: 0, topPct: 0, widthPct: 100, heightPct: 100 },
    highlight: null,
  }
  close(cropFrame({ x: 10, y: 10, width: 0, height: 50 }, PAGE_1000x2000), whole)
  close(cropFrame({ x: 10, y: 10, width: 50, height: -5 }, PAGE_1000x2000), whole)
  // Wholly outside the page: the clamped region has no area.
  close(cropFrame({ x: 2000, y: 10, width: 50, height: 50 }, PAGE_1000x2000), whole)
  assert.equal(fullSheetFrame({ x: 10, y: 10, width: 0, height: 0 }, PAGE_1000x2000).highlight, null)
})

test('the highlight lands on the object in the scan, whatever the framing', () => {
  // Map the highlight back through the image box to page pixels: it must be
  // the object's rectangle exactly.
  const page = { width: 1000, height: 2000 }
  const geometry = { x: 610, y: 1234, width: 120, height: 80 }
  const { image, highlight } = cropFrame(geometry, PAGE_1000x2000)
  assert.ok(highlight)
  const toPageX = (pct: number) => ((pct - image.leftPct) / image.widthPct) * page.width
  const toPageY = (pct: number) => ((pct - image.topPct) / image.heightPct) * page.height
  close(toPageX(highlight.leftPct), geometry.x, 'x')
  close(toPageY(highlight.topPct), geometry.y, 'y')
  close(toPageX(highlight.leftPct + highlight.widthPct), geometry.x + geometry.width, 'right')
  close(toPageY(highlight.topPct + highlight.heightPct), geometry.y + geometry.height, 'bottom')
})

test('the full sheet keeps the template aspect and draws the object on it', () => {
  close(fullSheetFrame({ x: 100, y: 500, width: 200, height: 50 }, PAGE_1000x2000), {
    aspectRatio: 0.5,
    image: { leftPct: 0, topPct: 0, widthPct: 100, heightPct: 100 },
    highlight: { leftPct: 10, topPct: 25, widthPct: 20, heightPct: 2.5 },
  })
})

// ── Review actions ───────────────────────────────────────────────────────────

test('editorKind: by result type, identity whenever the role or type says so', () => {
  assert.equal(editorKind({ result_type: 'QUESTION', role: null }), 'question')
  assert.equal(editorKind({ result_type: 'MATRIX', role: null }), 'matrix')
  assert.equal(editorKind({ result_type: 'FREE_TEXT', role: null }), 'freeText')
  assert.equal(editorKind({ result_type: 'CODE', role: null }), 'code')
  assert.equal(editorKind({ result_type: 'RESPONDENT_IDENTIFIER', role: null }), 'identifier')
  assert.equal(editorKind({ result_type: 'RESPONDENT_IDENTIFIER', role: 'RESPONDENT_IDENTIFIER' }), 'identifier')
  // A free-text area holding the role is an identity, not an answer.
  assert.equal(editorKind({ result_type: 'FREE_TEXT', role: 'RESPONDENT_IDENTIFIER' }), 'identifier')
})

const EDIT_QUESTION_CONFIG = {
  question_config: {
    question_type: 'SINGLE_CHOICE',
    options: [
      { id: 'o-c', order_index: 2, option_text: '20' },
      { id: 'o-a', order_index: 0, option_text: '16' },
      { id: 'o-b', order_index: 1, option_text: '18' },
    ],
  },
} as unknown as PageObjectDetail

const EDIT_MATRIX_CONFIG = {
  matrix_config: {
    rows_count: 2,
    cols_count: 3,
    selection_rule: 'ONE_PER_ROW',
    rows: [{ order_index: 0, label: 'Tea' }],
    cols: [{ order_index: 1, label: 'Often' }],
  },
} as unknown as PageObjectDetail

test('detectedOptionIds / detectedCells: what the extractor read, defensively', () => {
  assert.deepEqual(
    detectedOptionIds({ detected_payload: { selected_options: [{ option_id: 'o-b' }, { option_id: 'o-c' }] } }),
    ['o-b', 'o-c'],
  )
  assert.deepEqual(detectedOptionIds({ detected_payload: null }), [])
  assert.deepEqual(detectedOptionIds({ detected_payload: { selected_options: 'x' } }), [])
  assert.deepEqual(
    detectedCells({ detected_payload: { rows: [{ row_index: 1, selected_cells: [{ col_index: 2 }] }], cols: [] } }),
    new Set(['1:2']),
  )
  assert.deepEqual(
    detectedCells({ detected_payload: { rows: [], cols: [{ col_index: 0, selected_cells: [{ row_index: 1 }] }] } }),
    new Set(['1:0']),
  )
  assert.deepEqual(detectedCells({ detected_payload: null }), new Set())
})

test('initialEditorState: question from the reading, filtered by the config', () => {
  const o = obj('q', {
    detected_payload: { selected_options: [{ option_id: 'o-b' }, { option_id: 'gone' }] },
  })
  assert.deepEqual(initialEditorState(o).selectedOptionIds, ['o-b', 'gone'])
  assert.deepEqual(initialEditorState(o, EDIT_QUESTION_CONFIG).selectedOptionIds, ['o-b'])
})

test('initialEditorState: a SUBSTITUTED reading starts from the value entered', () => {
  const q = obj('q', {
    detected_payload: { selected_options: [{ option_id: 'o-b' }] },
    review_outcome: 'SUBSTITUTED',
    reviewed_payload: { selected_option_ids: ['o-a'], validity: 'VALID' },
  })
  assert.deepEqual(initialEditorState(q, EDIT_QUESTION_CONFIG).selectedOptionIds, ['o-a'])
  // Any other outcome: the reading, not the (absent) reviewed payload.
  const confirmed = obj('q2', {
    detected_payload: { selected_options: [{ option_id: 'o-b' }] },
    review_outcome: 'CONFIRMED',
  })
  assert.deepEqual(initialEditorState(confirmed).selectedOptionIds, ['o-b'])
})

test('initialEditorState: matrix cells from rows, cols or the reviewed groups', () => {
  const read = obj('m', {
    result_type: 'MATRIX',
    detected_payload: { rows: [{ row_index: 0, selected_cells: [{ col_index: 1 }] }, { row_index: 5, selected_cells: [{ col_index: 0 }] }], cols: [] },
  })
  assert.deepEqual(initialEditorState(read).cells, new Set(['0:1', '5:0']))
  // Out of the authored grid: dropped once the config is known.
  assert.deepEqual(initialEditorState(read, EDIT_MATRIX_CONFIG).cells, new Set(['0:1']))
  const byRow = obj('m2', {
    result_type: 'MATRIX',
    review_outcome: 'SUBSTITUTED',
    reviewed_payload: { rows: [{ row_index: 1, selected_col_indices: [0, 2], validity: 'TOO_MANY_ANSWERS' }], cols: [] },
  })
  assert.deepEqual(initialEditorState(byRow).cells, new Set(['1:0', '1:2']))
  const byCol = obj('m3', {
    result_type: 'MATRIX',
    review_outcome: 'SUBSTITUTED',
    reviewed_payload: { rows: [], cols: [{ col_index: 2, selected_row_indices: [1], validity: 'VALID' }] },
  })
  assert.deepEqual(initialEditorState(byCol).cells, new Set(['1:2']))
})

test('initialEditorState: identifier, free text, code', () => {
  const grid = obj('g', { result_type: 'RESPONDENT_IDENTIFIER', detected_payload: { raw_value: '1000?7' } })
  assert.equal(initialEditorState(grid).text, '1000?7')
  const fieldId = obj('f', {
    result_type: 'FREE_TEXT', role: 'RESPONDENT_IDENTIFIER', detected_payload: { suggested_text: 'AB12' },
  })
  assert.equal(initialEditorState(fieldId).text, 'AB12')
  const replacedId = obj('r', {
    result_type: 'RESPONDENT_IDENTIFIER',
    detected_payload: { raw_value: '1?' },
    review_outcome: 'SUBSTITUTED',
    reviewed_payload: { value: '17' },
  })
  assert.equal(initialEditorState(replacedId).text, '17')
  assert.equal(
    initialEditorState(obj('t', { result_type: 'FREE_TEXT', detected_payload: { suggested_text: 'Mario' } })).text,
    'Mario',
  )
  assert.equal(
    initialEditorState(obj('t2', {
      result_type: 'FREE_TEXT', review_outcome: 'SUBSTITUTED', reviewed_payload: { text: '' },
      detected_payload: { suggested_text: 'Mario' },
    })).text,
    '',
  )
  assert.equal(
    initialEditorState(obj('c', { result_type: 'CODE', detected_payload: { decoded_value: 'QR-1', decode_successful: true } })).text,
    'QR-1',
  )
  assert.equal(
    initialEditorState(obj('c2', { result_type: 'CODE', detected_payload: { decoded_value: null, decode_successful: false } })).text,
    '',
  )
  assert.equal(
    initialEditorState(obj('c3', {
      result_type: 'CODE', review_outcome: 'SUBSTITUTED', reviewed_payload: { value: 'X9' }, detected_payload: null,
    })).text,
    'X9',
  )
  // A placeholder (null payload) starts empty.
  assert.equal(initialEditorState(obj('p', { result_type: 'RESPONDENT_IDENTIFIER' })).text, '')
})

test('buildSubstituteValue: the request value per kind, trimmed as the BE trims', () => {
  const state = { selectedOptionIds: ['o-a', 'o-c'], cells: new Set(['1:0', '0:2', '0:1']), text: '  42  ' }
  assert.deepEqual(buildSubstituteValue('question', state), { selected_option_ids: ['o-a', 'o-c'] })
  assert.deepEqual(buildSubstituteValue('matrix', state), { selected_cells: [[0, 1], [0, 2], [1, 0]] })
  assert.deepEqual(buildSubstituteValue('identifier', state), { value: '42' })
  assert.deepEqual(buildSubstituteValue('freeText', state), { text: '42' })
  assert.deepEqual(buildSubstituteValue('freeText', { ...state, text: '   ' }), { text: '' })
  assert.deepEqual(buildSubstituteValue('code', state), { value: '42' })
  assert.deepEqual(
    buildSubstituteValue('question', { ...state, selectedOptionIds: [] }),
    { selected_option_ids: [] },
  )
  assert.equal(cellKey(3, 4), '3:4')
})

test('questionHint', () => {
  assert.deepEqual(questionHint('SINGLE_CHOICE', 0), { text: 'Counts as no answer', warn: true })
  assert.deepEqual(questionHint('MULTIPLE_CHOICE', 0), { text: 'Counts as no answer', warn: true })
  assert.deepEqual(questionHint('SINGLE_CHOICE', 2), {
    text: 'Counts as an invalid answer (more than one option on a single-choice question)',
    warn: true,
  })
  assert.deepEqual(questionHint('SINGLE_CHOICE', 1), { text: '1 option selected', warn: false })
  assert.deepEqual(questionHint('MULTIPLE_CHOICE', 3), { text: '3 options selected', warn: false })
})

test('matrixHint: row groups for row rules and FREE, column groups otherwise', () => {
  const cells = new Set(['0:0', '0:1'])
  assert.deepEqual(matrixHint('ONE_PER_ROW', cells, 3, 2), {
    text: '2 rows with no mark, 1 row with more than one mark', warn: true,
  })
  // MANY_PER_ROW / FREE: more than one is fine.
  assert.deepEqual(matrixHint('MANY_PER_ROW', cells, 1, 2), { text: 'All rows have at least one mark', warn: false })
  assert.deepEqual(matrixHint('FREE', cells, 2, 2), { text: '1 row with no mark', warn: true })
  assert.deepEqual(matrixHint('ONE_PER_COLUMN', cells, 1, 2), { text: 'All columns have one mark', warn: false })
  assert.deepEqual(matrixHint('ONE_PER_COLUMN', new Set(['0:0', '1:0']), 2, 2), {
    text: '1 column with no mark, 1 column with more than one mark', warn: true,
  })
  assert.deepEqual(matrixHint('MANY_PER_COLUMN', new Set(['0:0', '1:0']), 2, 2), {
    text: '1 column with no mark', warn: true,
  })
  assert.deepEqual(matrixHint('ONE_PER_ROW', new Set(['0:0', '1:1']), 2, 2), { text: 'All rows have one mark', warn: false })
})

test('formatReviewedValue: question letters, in option order', () => {
  const q = obj('q', { reviewed_payload: { selected_option_ids: ['o-c', 'o-a'], validity: 'TOO_MANY_ANSWERS' } })
  assert.equal(formatReviewedValue(q, EDIT_QUESTION_CONFIG), 'Replaced with: A, C')
  assert.equal(formatReviewedValue(q), 'Replaced with: 2 options')
  assert.equal(
    formatReviewedValue(obj('q1', { reviewed_payload: { selected_option_ids: ['o-b'], validity: 'VALID' } })),
    'Replaced with: 1 option',
  )
  assert.equal(
    formatReviewedValue(obj('q2', { reviewed_payload: { selected_option_ids: [], validity: 'NO_ANSWER' } })),
    'Replaced with: no option marked',
  )
  assert.equal(
    formatReviewedValue(obj('q3', { reviewed_payload: { selected_option_ids: ['gone'], validity: 'VALID' } }), EDIT_QUESTION_CONFIG),
    'Replaced with: ?',
  )
})

test('formatReviewedValue: matrix groups with labels, index fallback', () => {
  const rows = obj('m', {
    result_type: 'MATRIX',
    reviewed_payload: {
      rows: [
        { row_index: 0, selected_col_indices: [1], validity: 'VALID' },
        { row_index: 1, selected_col_indices: [], validity: 'NO_ANSWER' },
      ],
      cols: [],
    },
  })
  assert.equal(formatReviewedValue(rows, EDIT_MATRIX_CONFIG), 'Replaced with: Tea: Often; Row 2: no mark')
  assert.equal(formatReviewedValue(rows), 'Replaced with: Row 1: 2; Row 2: no mark')
  const cols = obj('m2', {
    result_type: 'MATRIX',
    reviewed_payload: { rows: [], cols: [{ col_index: 1, selected_row_indices: [0, 1], validity: 'VALID' }] },
  })
  assert.equal(formatReviewedValue(cols, EDIT_MATRIX_CONFIG), 'Replaced with: Often: Tea, 2')
  assert.equal(
    formatReviewedValue(obj('m3', { result_type: 'MATRIX', reviewed_payload: { rows: [], cols: [] } })),
    'Replaced',
  )
})

test('formatReviewedValue: identifier, free text, code, and the unexpected', () => {
  assert.equal(
    formatReviewedValue(obj('i', { result_type: 'RESPONDENT_IDENTIFIER', reviewed_payload: { value: '123' } })),
    'Replaced with: 123',
  )
  assert.equal(
    formatReviewedValue(obj('i2', { result_type: 'FREE_TEXT', role: 'RESPONDENT_IDENTIFIER', reviewed_payload: { value: 'AB' } })),
    'Replaced with: AB',
  )
  assert.equal(
    formatReviewedValue(obj('t', { result_type: 'FREE_TEXT', reviewed_payload: { text: 'Mario' } })),
    'Replaced with: “Mario”',
  )
  assert.equal(
    formatReviewedValue(obj('t2', { result_type: 'FREE_TEXT', reviewed_payload: { text: '' } })),
    'Replaced with: nothing written',
  )
  assert.equal(
    formatReviewedValue(obj('c', { result_type: 'CODE', reviewed_payload: { value: 'QR-9' } })),
    'Replaced with: QR-9',
  )
  assert.equal(formatReviewedValue(obj('n', { result_type: 'CODE', reviewed_payload: null })), 'Replaced')
  assert.equal(
    formatReviewedValue(obj('w', { result_type: 'QUESTION', reviewed_payload: { value: 'x' } })),
    'Replaced',
  )
})

test('outcomeLabel / outcomeIcon', () => {
  assert.equal(outcomeLabel('CONFIRMED'), 'Confirmed')
  assert.equal(outcomeLabel('SUBSTITUTED'), 'Replaced')
  assert.equal(outcomeLabel('FLAGGED'), 'Flagged as unresolvable')
  assert.equal(outcomeIcon('CONFIRMED'), '✓')
  assert.equal(outcomeIcon('SUBSTITUTED'), '✎')
  assert.equal(outcomeIcon('FLAGGED'), '⚑')
})

test('formatReviewedAt: time today, date and time otherwise, "" when unparsable', () => {
  const now = new Date(2026, 9, 4, 15, 0)
  const today = new Date(2026, 9, 4, 10, 42)
  const yesterday = new Date(2026, 9, 3, 10, 42)
  const time = today.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  assert.equal(formatReviewedAt(today.toISOString(), now), time)
  assert.equal(formatReviewedAt(yesterday.toISOString(), now), `${yesterday.toLocaleDateString()} ${time}`)
  assert.equal(formatReviewedAt('not a date', now), '')
})

test('nextUnresolvedResultId: after the current one, in list order, wrapping', () => {
  const detail = {
    pages: [
      page(0, 1, [obj('a'), obj('b', { resolved: true }), obj('c')]),
      page(1, null, []),
      page(2, 2, [obj('d', { resolved: true }), obj('e')]),
    ],
  }
  assert.equal(nextUnresolvedResultId(detail, 'a'), 'c')
  assert.equal(nextUnresolvedResultId(detail, 'c'), 'e')   // across sheets
  assert.equal(nextUnresolvedResultId(detail, 'e'), 'a')   // wraps
  assert.equal(nextUnresolvedResultId(detail, 'b'), 'c')   // from a resolved one
  assert.equal(nextUnresolvedResultId(detail, 'zz'), 'a')  // current unknown: the first
  assert.equal(nextUnresolvedResultId(detail, null), 'a')
  // Never the current one itself, even if still unresolved.
  assert.equal(nextUnresolvedResultId({ pages: [page(0, 1, [obj('a')])] }, 'a'), null)
  assert.equal(
    nextUnresolvedResultId({ pages: [page(0, 1, [obj('a', { resolved: true }), obj('b', { resolved: true })])] }, 'a'),
    null,
  )
  assert.equal(nextUnresolvedResultId({ pages: [] }, null), null)
})

// ── Keyboard flow ────────────────────────────────────────────────────────────

test('adjacentResultId: list order across sheets, no wrap', () => {
  const detail = {
    pages: [
      page(0, 1, [obj('a'), obj('b')]),
      page(1, null, []),
      page(2, 2, [obj('c')]),
    ],
  }
  assert.equal(adjacentResultId(detail, 'a', 'next'), 'b')
  assert.equal(adjacentResultId(detail, 'b', 'next'), 'c')
  assert.equal(adjacentResultId(detail, 'c', 'next'), null)
  assert.equal(adjacentResultId(detail, 'c', 'prev'), 'b')
  assert.equal(adjacentResultId(detail, 'a', 'prev'), null)
  assert.equal(adjacentResultId(detail, 'zz', 'next'), null)
  assert.equal(adjacentResultId(detail, null, 'prev'), null)
})

function key(k: string, extra: Partial<ReviewKeyEvent> = {}): ReviewKeyEvent {
  return {
    key: k, ctrlKey: false, metaKey: false, altKey: false, repeat: false,
    defaultPrevented: false, targetIsField: false, targetIsControl: false, ...extra,
  }
}

const UNRESOLVED = { resolved: false, confirmable: true }
const RESOLVED = { resolved: true, confirmable: true }

function ctx(extra: Partial<ReviewKeyContext> = {}): ReviewKeyContext {
  return {
    lockMine: true, scanned: true, editorOpen: false, noteOpen: false,
    dialogOpen: false, pending: false, selected: UNRESOLVED, ...extra,
  }
}

test('reviewKeyAction: Enter confirms an unresolved, confirmable reading', () => {
  assert.equal(reviewKeyAction(key('Enter'), ctx()), 'confirm')
  assert.equal(reviewKeyAction(key('Enter'), ctx({ selected: RESOLVED })), null)
  assert.equal(reviewKeyAction(key('Enter'), ctx({ selected: { resolved: false, confirmable: false } })), null)
  assert.equal(reviewKeyAction(key('Enter'), ctx({ selected: null })), null)
  assert.equal(reviewKeyAction(key('Enter'), ctx({ editorOpen: true })), null)
  assert.equal(reviewKeyAction(key('Enter'), ctx({ lockMine: false })), null)
  assert.equal(reviewKeyAction(key('Enter'), ctx({ scanned: false })), null)
  // A focused button keeps Enter for its own click.
  assert.equal(reviewKeyAction(key('Enter', { targetIsControl: true }), ctx()), null)
  // With the note open (focus elsewhere) Enter still confirms: the note goes with it.
  assert.equal(reviewKeyAction(key('Enter'), ctx({ noteOpen: true })), 'confirm')
})

test('reviewKeyAction: R replaces, F flags, U undoes — lock, status, editor', () => {
  for (const k of ['r', 'R']) {
    assert.equal(reviewKeyAction(key(k), ctx()), 'replace')
    assert.equal(reviewKeyAction(key(k), ctx({ selected: RESOLVED })), 'replace')
    assert.equal(reviewKeyAction(key(k), ctx({ editorOpen: true })), null)
    assert.equal(reviewKeyAction(key(k), ctx({ lockMine: false })), null)
    assert.equal(reviewKeyAction(key(k), ctx({ selected: null })), null)
  }
  for (const k of ['f', 'F']) {
    assert.equal(reviewKeyAction(key(k), ctx()), 'flag')
    assert.equal(reviewKeyAction(key(k), ctx({ selected: RESOLVED })), 'flag')
    assert.equal(reviewKeyAction(key(k), ctx({ editorOpen: true })), null)
    assert.equal(reviewKeyAction(key(k), ctx({ scanned: false })), null)
  }
  for (const k of ['u', 'U']) {
    assert.equal(reviewKeyAction(key(k), ctx({ selected: RESOLVED })), 'undo')
    assert.equal(reviewKeyAction(key(k), ctx()), null)
    assert.equal(reviewKeyAction(key(k), ctx({ selected: RESOLVED, editorOpen: true })), null)
    assert.equal(reviewKeyAction(key(k), ctx({ selected: RESOLVED, lockMine: false })), null)
  }
  // Not confirmable: replace and flag still work, Enter does not.
  const broken = ctx({ selected: { resolved: false, confirmable: false } })
  assert.equal(reviewKeyAction(key('r'), broken), 'replace')
  assert.equal(reviewKeyAction(key('f'), broken), 'flag')
})

test('reviewKeyAction: arrows and J / K navigate, read-only views too', () => {
  for (const k of ['ArrowDown', 'j', 'J']) {
    assert.equal(reviewKeyAction(key(k), ctx()), 'next')
    assert.equal(reviewKeyAction(key(k), ctx({ lockMine: false, scanned: false })), 'next')
    assert.equal(reviewKeyAction(key(k), ctx({ selected: null })), 'next')
    // Selecting away would drop the replacement being typed.
    assert.equal(reviewKeyAction(key(k), ctx({ editorOpen: true })), null)
  }
  for (const k of ['ArrowUp', 'k', 'K']) {
    assert.equal(reviewKeyAction(key(k), ctx()), 'prev')
    assert.equal(reviewKeyAction(key(k), ctx({ lockMine: false })), 'prev')
    assert.equal(reviewKeyAction(key(k), ctx({ editorOpen: true })), null)
  }
  // Arrows from a focused button (e.g. a list row) still navigate.
  assert.equal(reviewKeyAction(key('ArrowDown', { targetIsControl: true }), ctx()), 'next')
})

test('reviewKeyAction: Escape closes an open editor or note, nothing otherwise', () => {
  assert.equal(reviewKeyAction(key('Escape'), ctx({ editorOpen: true })), 'escape')
  assert.equal(reviewKeyAction(key('Escape'), ctx({ noteOpen: true })), 'escape')
  assert.equal(reviewKeyAction(key('Escape'), ctx()), null)
  // Read-only views have no editor or note to close.
  assert.equal(reviewKeyAction(key('Escape'), ctx({ lockMine: false })), null)
})

test('reviewKeyAction: ignored with modifiers, dialogs, pending saves, fields, handled keys', () => {
  const all = ['Enter', 'r', 'f', 'ArrowDown', 'ArrowUp', 'j', 'k']
  for (const k of all) {
    assert.equal(reviewKeyAction(key(k, { ctrlKey: true }), ctx()), null, `${k}+ctrl`)
    assert.equal(reviewKeyAction(key(k, { metaKey: true }), ctx()), null, `${k}+meta`)
    assert.equal(reviewKeyAction(key(k, { altKey: true }), ctx()), null, `${k}+alt`)
    assert.equal(reviewKeyAction(key(k), ctx({ dialogOpen: true })), null, `${k} dialog`)
    assert.equal(reviewKeyAction(key(k), ctx({ pending: true })), null, `${k} pending`)
    assert.equal(reviewKeyAction(key(k, { targetIsField: true }), ctx()), null, `${k} field`)
    assert.equal(reviewKeyAction(key(k, { defaultPrevented: true }), ctx()), null, `${k} handled`)
  }
  assert.equal(reviewKeyAction(key('u', { ctrlKey: true }), ctx({ selected: RESOLVED })), null)
  assert.equal(reviewKeyAction(key('u'), ctx({ selected: RESOLVED, pending: true })), null)
  assert.equal(reviewKeyAction(key('Escape', { targetIsField: true }), ctx({ editorOpen: true })), null)
  assert.equal(reviewKeyAction(key('Escape'), ctx({ editorOpen: true, dialogOpen: true })), null)
  // Unmapped keys.
  assert.equal(reviewKeyAction(key('x'), ctx()), null)
  assert.equal(reviewKeyAction(key(' '), ctx()), null)
  assert.equal(reviewKeyAction(key('Tab'), ctx()), null)
})

test('reviewKeyAction: a held-down Enter / F / U never repeats a save', () => {
  assert.equal(reviewKeyAction(key('Enter', { repeat: true }), ctx()), null)
  assert.equal(reviewKeyAction(key('f', { repeat: true }), ctx()), null)
  assert.equal(reviewKeyAction(key('u', { repeat: true }), ctx({ selected: RESOLVED })), null)
  // Navigation may repeat: holding ↓ walks the list.
  assert.equal(reviewKeyAction(key('ArrowDown', { repeat: true }), ctx()), 'next')
  assert.equal(reviewKeyAction(key('r', { repeat: true }), ctx()), 'replace')
})

test('candidateNote: a later identity reading names the sheet that sets the candidate', () => {
  const first = { source_page_order: 1, template_page: { id: 'tp2', page_index: 2, width_px: null, height_px: null } }
  const grid = { result_type: 'RESPONDENT_IDENTIFIER' as const, role: 'RESPONDENT_IDENTIFIER' as const, sets_candidate: false }
  assert.equal(
    candidateNote(grid, first, true),
    'Does not set the candidate: the reading on sheet 2 (page 2) does.',
  )
  // A free text holding the role is an identity too.
  const text = { result_type: 'FREE_TEXT' as const, role: 'RESPONDENT_IDENTIFIER' as const, sets_candidate: false }
  assert.equal(
    candidateNote(text, { source_page_order: 0, template_page: null }, true),
    'Does not set the candidate: the reading on sheet 1 does.',
  )
  assert.equal(
    candidateNote(grid, null, true),
    'Does not set the candidate: only the first reading in page order does.',
  )
})

test('candidateNote: nothing to say on the first identity reading or on an answer', () => {
  const page = { source_page_order: 0, template_page: null }
  assert.equal(
    candidateNote({ result_type: 'RESPONDENT_IDENTIFIER', role: 'RESPONDENT_IDENTIFIER', sets_candidate: true }, page, true),
    null,
  )
  assert.equal(candidateNote({ result_type: 'FREE_TEXT', role: null, sets_candidate: false }, page, true), null)
  assert.equal(candidateNote({ result_type: 'QUESTION', role: null, sets_candidate: false }, page, true), null)
})

test('candidateNote: a first reading out of review points to the header', () => {
  const first = { source_page_order: 0, template_page: { id: 'tp1', page_index: 1, width_px: null, height_px: null } }
  assert.equal(
    candidateNote({ result_type: 'RESPONDENT_IDENTIFIER', role: 'RESPONDENT_IDENTIFIER', sets_candidate: false }, first, false),
    'Does not set the candidate: the reading on sheet 1 (page 1) does, and is not in review — edit the candidate in the header.',
  )
})

// ── Closing the submission ───────────────────────────────────────────────────

const closable = (extra: Partial<Parameters<typeof closeWarnings>[0]> = {}) => ({
  pending_review_count: 0,
  respondent_identifier: '100004',
  respondent_identifier_expected: true,
  missing_page_indices: [] as number[],
  ...extra,
})

test('closeWarnings is empty when nothing is worth seeing: the click closes', () => {
  assert.deepEqual(closeWarnings(closable()), [])
})

test('closeWarnings counts the unresolved readings, singular and plural', () => {
  assert.deepEqual(closeWarnings(closable({ pending_review_count: 1 })), [{
    title: '1 object is still unresolved.',
    detail: 'It will count as no answer (0 points) and as not reviewed in the export.',
  }])
  assert.deepEqual(closeWarnings(closable({ pending_review_count: 2 })), [{
    title: '2 objects are still unresolved.',
    detail: 'They will count as no answer (0 points) and as not reviewed in the export.',
  }])
})

test('closeWarnings flags no candidate only where the variant expects one', () => {
  assert.deepEqual(closeWarnings(closable({ respondent_identifier: null })).map(w => w.title), [
    'No candidate.',
  ])
  // An anonymous questionnaire has no candidate to miss.
  assert.deepEqual(
    closeWarnings(closable({ respondent_identifier: null, respondent_identifier_expected: false })),
    [],
  )
})

test('closeWarnings lists the missing pages', () => {
  assert.deepEqual(closeWarnings(closable({ missing_page_indices: [3] })), [
    { title: 'Missing page: 3.', detail: 'Its objects have no reading.' },
  ])
  assert.deepEqual(closeWarnings(closable({ missing_page_indices: [2, 3] })).map(w => w.title), [
    'Missing pages: 2, 3.',
  ])
})

test('closeWarnings lists every warning that applies, in a fixed order', () => {
  const all = closable({ pending_review_count: 2, respondent_identifier: null, missing_page_indices: [3] })
  assert.deepEqual(closeWarnings(all).map(w => w.title), [
    '2 objects are still unresolved.', 'No candidate.', 'Missing page: 3.',
  ])
})

test('reviewCountLine: remaining while open, unresolved at closing once closed', () => {
  assert.deepEqual(reviewCountLine({ status: 'SCANNED', pending_review_count: 2, review_count: 6 }), {
    label: 'To review', value: '2 of 6 remaining',
  })
  for (const status of ['REVIEWED', 'FINALIZED'] as const) {
    assert.deepEqual(reviewCountLine({ status, pending_review_count: 2, review_count: 6 }), {
      label: 'Unresolved', value: '2 of 6 at closing',
    })
  }
})

test('reviewCountLine: back to remaining once a closed submission is reopened', () => {
  // The response of the reopen: SCANNED again, the same counts.
  assert.deepEqual(reviewCountLine({ status: 'SCANNED', pending_review_count: 2, review_count: 6 }), {
    label: 'To review', value: '2 of 6 remaining',
  })
})

test('canReopen: only a REVIEWED submission', () => {
  assert.equal(canReopen('REVIEWED'), true)
  for (const status of [
    'UPLOADED', 'MATCHED', 'MATCH_FAILED', 'UNASSIGNED', 'UNRESOLVABLE', 'SCANNED', 'FINALIZED',
  ] as const) {
    assert.equal(canReopen(status), false, status)
  }
})
