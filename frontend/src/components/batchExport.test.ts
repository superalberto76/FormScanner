// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  DEFAULT_EXPORT_CHOICE,
  EXPORT_CHOICES,
  downloadFileName,
  exportBlockedLine,
  exportChoiceInfo,
  exportFailedText,
  exportKindInfo,
  fileNameError,
  parseExportChoice,
  proposedDetailName,
  proposedExportName,
  proposedSummaryName,
} from './batchExport.ts'
import type { ExportReadiness } from '../api/domain'

const ready: ExportReadiness = { ready: true, to_extract: 0, in_review: 0, to_assign: 0, unassigned: 0 }
const notReady = (counts: Partial<ExportReadiness>): ExportReadiness => ({
  ...ready,
  ready: false,
  ...counts,
})

// ── Choices ─────────────────────────────────────────────────────────────────

test('the three choices map to format, dialect and extension', () => {
  assert.deepEqual(
    EXPORT_CHOICES.map(c => [c.value, c.format, c.dialect, c.extension]),
    [
      ['csv-comma', 'csv', 'comma', 'csv'],
      ['csv-semicolon', 'csv', 'semicolon', 'csv'],
      ['xlsx', 'xlsx', 'comma', 'xlsx'],
    ],
  )
})

test('the default is the comma CSV', () => {
  assert.equal(DEFAULT_EXPORT_CHOICE, 'csv-comma')
  assert.equal(exportChoiceInfo(DEFAULT_EXPORT_CHOICE).label, 'CSV — comma separated')
})

test('a remembered choice is read back, anything else is the default', () => {
  assert.equal(parseExportChoice('xlsx'), 'xlsx')
  assert.equal(parseExportChoice('csv-semicolon'), 'csv-semicolon')
  assert.equal(parseExportChoice(null), 'csv-comma')
  assert.equal(parseExportChoice('pdf'), 'csv-comma')
})

// ── File name ───────────────────────────────────────────────────────────────

test('the proposed name has the batch name and the UTC time', () => {
  // 15:30 in Rome is 14:30 UTC.
  const now = new Date('2026-11-03T15:30:00+01:00')
  assert.equal(proposedSummaryName('Kit PDF', now), 'Summary_Kit_PDF_20261103-1430Z')
})

test('the proposed name cleans the batch name like the server', () => {
  const now = new Date('2026-11-03T14:30:00Z')
  assert.equal(proposedSummaryName("Teacher's exam / 3ª", now), 'Summary_Teacher_s_exam_3ª_20261103-1430Z')
  assert.equal(proposedSummaryName('Mid-term exam', now), 'Summary_Mid-term_exam_20261103-1430Z')
  assert.equal(proposedSummaryName('///', now), 'Summary_batch_20261103-1430Z')
})

test('the detail name has its own prefix and the same scheme', () => {
  const now = new Date('2026-11-03T15:30:00+01:00')
  assert.equal(proposedDetailName('Kit PDF', now), 'Detail_Kit_PDF_20261103-1430Z')
  assert.equal(proposedDetailName('///', now), 'Detail_batch_20261103-1430Z')
  assert.equal(proposedExportName('summary', 'Kit PDF', now), proposedSummaryName('Kit PDF', now))
})

// ── Kinds ───────────────────────────────────────────────────────────────────

test('each export has its button, title and description', () => {
  assert.deepEqual(
    [exportKindInfo('summary').button, exportKindInfo('summary').title, exportKindInfo('summary').rowsSuffix],
    ['Export summary…', 'Export summary', '.'],
  )
  assert.deepEqual(
    [exportKindInfo('detail').button, exportKindInfo('detail').title, exportKindInfo('detail').rowsSuffix],
    ['Export detail…', 'Export detail', ', one column per question.'],
  )
})

test('an empty name is refused', () => {
  assert.equal(fileNameError(''), 'Enter a file name.')
  assert.equal(fileNameError('   '), 'Enter a file name.')
})

test('a name with a character a file name cannot hold is refused', () => {
  for (const name of ['Results 3/11', 'a\\b', 'Kit: PDF', 'a*', 'why?', 'say "hi"', '<x>', 'a|b']) {
    assert.equal(
      fileNameError(name),
      'A file name cannot contain these characters: / \\ : * ? " < > |',
      name,
    )
  }
})

test('an ordinary name, accents and dashes included, is accepted', () => {
  assert.equal(fileNameError('Test kit PDF — results'), null)
  assert.equal(fileNameError('Città_2026-11-03'), null)
})

test('the saved name is trimmed and takes the extension of the choice', () => {
  assert.equal(downloadFileName('  Results  ', 'csv-semicolon'), 'Results.csv')
  assert.equal(downloadFileName('Results', 'xlsx'), 'Results.xlsx')
})

// ── Why not ready ───────────────────────────────────────────────────────────

test('a ready batch has no line', () => {
  assert.equal(exportBlockedLine(ready, 22), null)
})

test('every reason above zero is listed, in order', () => {
  assert.deepEqual(
    exportBlockedLine(notReady({ to_extract: 1, in_review: 14, to_assign: 2, unassigned: 1 }), 22),
    { text: 'Not ready: 1 to extract · 14 in review · 2 to assign · 1 unassigned', tone: 'warning' },
  )
  assert.deepEqual(exportBlockedLine(notReady({ in_review: 3 }), 22), {
    text: 'Not ready: 3 in review',
    tone: 'warning',
  })
  assert.deepEqual(exportBlockedLine(notReady({ unassigned: 2, to_extract: 1 }), 5), {
    text: 'Not ready: 1 to extract · 2 unassigned',
    tone: 'warning',
  })
})

test('a batch of unresolvable submissions only has nothing to export', () => {
  assert.deepEqual(exportBlockedLine(notReady({}), 1), {
    text: 'Nothing to export: no submission has been closed by a review.',
    tone: 'muted',
  })
})

test('an empty batch has nothing to export yet', () => {
  assert.deepEqual(exportBlockedLine(notReady({}), 0), {
    text: 'Nothing to export yet: upload and review submissions first.',
    tone: 'muted',
  })
})

test('the failure text ends the reason with a full stop and asks to try again', () => {
  assert.equal(exportFailedText('HTTP 502'), 'The export failed: HTTP 502. Try again.')
  assert.equal(
    exportFailedText('Something went wrong.'),
    'The export failed: Something went wrong. Try again.',
  )
})
