// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  extractionResultLines,
  formatThresholdPercent,
  parseThresholdPercentInput,
} from './batchQueue.ts'
import type { ExtractionRunSummary } from '../api/domain.ts'

function run(extra: Partial<ExtractionRunSummary> = {}): ExtractionRunSummary {
  return {
    submissions_processed: 0,
    pages_processed: 0,
    results_created: 0,
    submissions_scanned: 0,
    skipped_already_extracted: 0,
    skipped_not_applicable: 0,
    skipped_not_yet_supported: 0,
    errors: 0,
    error_details: [],
    ...extra,
  }
}

// ── extraction result ────────────────────────────────────────────────────────
test('the main line reports the three counters with thousands separators', () => {
  const lines = extractionResultLines(
    run({ submissions_processed: 38, results_created: 1140, submissions_scanned: 36 }),
  )
  assert.equal(
    lines.main,
    'Last run: 38 submissions processed · 1,140 results created · 36 ready for review',
  )
  assert.equal(lines.errors, null)
})

test('singular forms for exactly one', () => {
  const lines = extractionResultLines(
    run({ submissions_processed: 1, results_created: 1, submissions_scanned: 1 }),
  )
  assert.equal(
    lines.main,
    'Last run: 1 submission processed · 1 result created · 1 ready for review',
  )
})

test('large counts of submissions and scanned are separated too', () => {
  const lines = extractionResultLines(
    run({ submissions_processed: 1200, results_created: 5, submissions_scanned: 1001 }),
  )
  assert.match(lines.main, /^Last run: 1,200 submissions processed/)
  assert.match(lines.main, /1,001 ready for review$/)
})

test('a run that read nothing and failed nothing says so', () => {
  const lines = extractionResultLines(run({ submissions_processed: 42 }))
  assert.equal(
    lines.main,
    'Last run: 42 submissions processed · 0 results created · 0 ready for review — nothing new to read.',
  )
  assert.equal(lines.errors, null)
})

test('nothing created but errors: no "nothing new" suffix, an errors line instead', () => {
  const lines = extractionResultLines(run({ submissions_processed: 3, errors: 2 }))
  assert.doesNotMatch(lines.main, /nothing new/)
  assert.equal(
    lines.errors,
    '2 errors — those submissions stay in “To extract”; run again to retry.',
  )
})

test('one error is singular', () => {
  const lines = extractionResultLines(run({ results_created: 10, errors: 1 }))
  assert.equal(lines.errors, '1 error — those submissions stay in “To extract”; run again to retry.')
})

test('thousands of errors are separated', () => {
  const lines = extractionResultLines(run({ errors: 2500 }))
  assert.match(lines.errors ?? '', /^2,500 errors — /)
})

// ── threshold format ─────────────────────────────────────────────────────────
test('formatThresholdPercent drops a trailing zero', () => {
  assert.equal(formatThresholdPercent(0.8), '80%')
  assert.equal(formatThresholdPercent(0), '0%')
  assert.equal(formatThresholdPercent(1), '100%')
})

test('formatThresholdPercent keeps one decimal when there is one', () => {
  assert.equal(formatThresholdPercent(0.825), '82.5%')
  assert.equal(formatThresholdPercent(0.07), '7%')
})

test('formatThresholdPercent rounds to at most one decimal', () => {
  assert.equal(formatThresholdPercent(0.8234), '82.3%')
  assert.equal(formatThresholdPercent(0.8299), '83%')
})

// ── threshold input ──────────────────────────────────────────────────────────
test('parseThresholdPercentInput returns the fraction for 0–100', () => {
  assert.equal(parseThresholdPercentInput('85'), 0.85)
  assert.equal(parseThresholdPercentInput('0'), 0)
  assert.equal(parseThresholdPercentInput('100'), 1)
  assert.equal(parseThresholdPercentInput(' 42 '), 0.42)
})

test('parseThresholdPercentInput rejects empty and blank', () => {
  assert.equal(parseThresholdPercentInput(''), null)
  assert.equal(parseThresholdPercentInput('   '), null)
})

test('parseThresholdPercentInput rejects decimals', () => {
  assert.equal(parseThresholdPercentInput('82.5'), null)
  assert.equal(parseThresholdPercentInput('80,5'), null)
})

test('parseThresholdPercentInput rejects out-of-range values', () => {
  assert.equal(parseThresholdPercentInput('101'), null)
  assert.equal(parseThresholdPercentInput('-1'), null)
  assert.equal(parseThresholdPercentInput('1000'), null)
})

test('parseThresholdPercentInput rejects non-numeric input', () => {
  assert.equal(parseThresholdPercentInput('abc'), null)
  assert.equal(parseThresholdPercentInput('80%'), null)
  assert.equal(parseThresholdPercentInput('1e2'), null)
})
