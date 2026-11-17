// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  classifyFiles,
  ingestSummaryLine,
  MAX_SELECTED_FILES,
  pluralize,
  selectionWarning,
  SUBMISSION_UPLOAD_ACCEPT,
  totalBytes,
  totalCount,
  type ClassifiedFiles,
} from './batchUpload.ts'
import type { SubmissionBatchIngestResult } from '../api/domain.ts'

// Duck-typed File: the helpers only read .type/.size/.name/.lastModified, so a
// literal avoids allocating real (and, for the size branch, huge) blobs.
function fakeFile(name: string, type: string, size = 1024, lastModified = 1): File {
  return { name, type, size, lastModified } as unknown as File
}

const EMPTY: ClassifiedFiles = { pdfFiles: [], imageFiles: [], ignoredCount: 0 }

function ingest(over: Partial<SubmissionBatchIngestResult> = {}): SubmissionBatchIngestResult {
  return {
    submissions_created: [],
    submissions_updated: [],
    pages_added: 0,
    unassigned_count: 0,
    rejected: [],
    ...over,
  }
}

// ── classifyFiles ────────────────────────────────────────────────────────────
test('classifyFiles splits PDFs from images by mime', () => {
  const out = classifyFiles([
    fakeFile('a.pdf', 'application/pdf'),
    fakeFile('b.png', 'image/png'),
    fakeFile('c.jpg', 'image/jpeg'),
  ])
  assert.deepEqual(out.pdfFiles.map(f => f.name), ['a.pdf'])
  assert.deepEqual(out.imageFiles.map(f => f.name), ['b.png', 'c.jpg'])
  assert.equal(out.ignoredCount, 0)
})

test('classifyFiles ignores unsupported types instead of failing', () => {
  const out = classifyFiles([
    fakeFile('notes.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
    fakeFile('scan.bmp', 'image/bmp'),   // fine for a PageObject IMAGE, not for scanning
    fakeFile('ok.pdf', 'application/pdf'),
  ])
  assert.equal(out.ignoredCount, 2)
  assert.equal(out.pdfFiles.length, 1)
  assert.equal(out.imageFiles.length, 0)
})

test('classifyFiles keeps arrival order — it is the last-resort page order', () => {
  const out = classifyFiles([
    fakeFile('p3.jpg', 'image/jpeg'),
    fakeFile('p1.jpg', 'image/jpeg'),
    fakeFile('p2.jpg', 'image/jpeg'),
  ])
  assert.deepEqual(out.imageFiles.map(f => f.name), ['p3.jpg', 'p1.jpg', 'p2.jpg'])
})

test('classifyFiles appends to an existing selection', () => {
  const first = classifyFiles([fakeFile('a.pdf', 'application/pdf')])
  const out = classifyFiles([fakeFile('b.png', 'image/png')], first)
  assert.deepEqual(out.pdfFiles.map(f => f.name), ['a.pdf'])
  assert.deepEqual(out.imageFiles.map(f => f.name), ['b.png'])
})

test('classifyFiles drops a re-dropped file rather than uploading it twice', () => {
  const same = () => fakeFile('a.pdf', 'application/pdf', 2048, 99)
  const first = classifyFiles([same()])
  const out = classifyFiles([same(), fakeFile('a.pdf', 'application/pdf', 4096, 99)], first)
  // Same name+size+mtime is the same file; a different size is a different one.
  assert.equal(out.pdfFiles.length, 2)
})

test('classifyFiles carries the ignored count across drops', () => {
  const first = classifyFiles([fakeFile('x.docx', 'application/msword')])
  const out = classifyFiles([fakeFile('y.txt', 'text/plain')], first)
  assert.equal(out.ignoredCount, 2)
})

// ── totals ───────────────────────────────────────────────────────────────────
test('totalCount and totalBytes span both paths', () => {
  const sel = classifyFiles([
    fakeFile('a.pdf', 'application/pdf', 100),
    fakeFile('b.png', 'image/png', 200),
  ])
  assert.equal(totalCount(sel), 2)
  assert.equal(totalBytes(sel), 300)
})

// ── selectionWarning ─────────────────────────────────────────────────────────
test('selectionWarning is null within both ceilings', () => {
  assert.equal(selectionWarning(classifyFiles([fakeFile('a.pdf', 'application/pdf')])), null)
  assert.equal(selectionWarning(EMPTY), null)
})

test('selectionWarning fires over the file-count ceiling', () => {
  const many = Array.from({ length: MAX_SELECTED_FILES + 1 }, (_, i) =>
    fakeFile(`p${i}.jpg`, 'image/jpeg', 10, i))
  const warning = selectionWarning(classifyFiles(many))
  assert.ok(warning?.includes(`${MAX_SELECTED_FILES + 1} files`))
})

test('selectionWarning fires over the byte ceiling', () => {
  const huge = classifyFiles([fakeFile('big.pdf', 'application/pdf', 400 * 1024 * 1024)])
  const warning = selectionWarning(huge)
  assert.ok(warning?.includes('400 MB'))
})

// ── labels ───────────────────────────────────────────────────────────────────
test('pluralize agrees with the count', () => {
  assert.equal(pluralize(1, 'file'), '1 file')
  assert.equal(pluralize(0, 'file'), '0 files')
  assert.equal(pluralize(2, 'file'), '2 files')
})

test('ingestSummaryLine reports all four counters, zeros included', () => {
  assert.equal(
    ingestSummaryLine(ingest({
      submissions_created: ['a', 'b'],
      submissions_updated: ['c'],
      unassigned_count: 1,
      rejected: [{ filename: 'x.jpg', reason: 'not a real image' }],
    })),
    '2 new submissions · 1 updated · 1 unassigned · 1 rejected',
  )
  assert.equal(ingestSummaryLine(ingest()), '0 new submissions · 0 updated · 0 unassigned · 0 rejected')
})

// ── accept ───────────────────────────────────────────────────────────────────
test('the accept string covers both ingestion paths and nothing else', () => {
  assert.equal(SUBMISSION_UPLOAD_ACCEPT, 'application/pdf,image/png,image/jpeg')
})
