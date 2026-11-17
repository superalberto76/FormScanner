// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Unit tests for the pure score formatters. Runs on Node's
// built-in test runner: node --test 'src/**/*.test.ts'.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { formatScore, formatScoreTriple } from './scoreFormat.ts'

test('a whole score shows without decimals', () => {
  assert.equal(formatScore(18), '18')
  assert.equal(formatScore(0), '0')
})

test('decimals are kept up to two, trailing zeros dropped', () => {
  assert.equal(formatScore(17.5), '17.5')
  assert.equal(formatScore(2.25), '2.25')
  assert.equal(formatScore(1 / 3), '0.33')
  assert.equal(formatScore(2 / 3), '0.67')
})

test('float noise does not leak into the display', () => {
  assert.equal(formatScore(0.1 + 0.2), '0.3')
})

test('a negative score keeps its sign', () => {
  assert.equal(formatScore(-1.5), '-1.5')
})

test('a value that rounds to zero is "0", never "-0"', () => {
  assert.equal(formatScore(-0.001), '0')
  assert.equal(formatScore(-0), '0')
})

test('the triple shows points, maximum and a one-decimal percentage', () => {
  assert.equal(
    formatScoreTriple({ raw_score: 12, max_possible_score: 18, percentage: 200 / 3 }),
    '12 / 18 (66.7%)',
  )
})

test('a whole percentage shows without decimals', () => {
  assert.equal(formatScoreTriple({ raw_score: 3, max_possible_score: 6, percentage: 50 }), '3 / 6 (50%)')
  assert.equal(formatScoreTriple({ raw_score: 0, max_possible_score: 6, percentage: 0 }), '0 / 6 (0%)')
})

test('with a maximum of 0 there is no percentage', () => {
  assert.equal(formatScoreTriple({ raw_score: 0, max_possible_score: 0, percentage: null }), '0 / 0 (—)')
})

test('a negative total keeps its sign in both parts', () => {
  assert.equal(
    formatScoreTriple({ raw_score: -2, max_possible_score: 18, percentage: -100 / 9 }),
    '-2 / 18 (-11.1%)',
  )
})

test('the points are formatted as a score', () => {
  assert.equal(
    formatScoreTriple({ raw_score: 17.5, max_possible_score: 1 / 3 * 60, percentage: 87.5 }),
    '17.5 / 20 (87.5%)',
  )
})

test('a percentage that rounds to zero is "0%", never "-0%"', () => {
  assert.equal(
    formatScoreTriple({ raw_score: -0.001, max_possible_score: 100, percentage: -0.001 }),
    '0 / 100 (0%)',
  )
})

test('a half percentage rounds away from zero on both signs', () => {
  assert.equal(formatScoreTriple({ raw_score: 2, max_possible_score: 16, percentage: 12.25 }), '2 / 16 (12.3%)')
  assert.equal(formatScoreTriple({ raw_score: -2, max_possible_score: 16, percentage: -12.25 }), '-2 / 16 (-12.3%)')
})
