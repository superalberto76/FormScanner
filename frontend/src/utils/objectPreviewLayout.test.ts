// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Unit tests for the faithful-preview decision logic. DOM-free, so
// they run on Node's built-in runner with TS type-stripping:
//   node --test 'src/**/*.test.ts'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { isCircleFilled, matrixLabel, visibleQuestionOptionCount } from './objectPreviewLayout.ts'

test('isCircleFilled: only SOLUTION + correct fills (STUDENT never fills)', () => {
  assert.equal(isCircleFilled('SOLUTION', true), true)
  assert.equal(isCircleFilled('SOLUTION', false), false)
  assert.equal(isCircleFilled('STUDENT', true), false) // STUDENT shows blanks
  assert.equal(isCircleFilled('STUDENT', false), false)
})

test('visibleQuestionOptionCount clips like the export break (HEADER_H=18, OPTION_H=18)', () => {
  const v = (h: number, n: number) => visibleQuestionOptionCount(h, n, 18, 18)
  // height 18 → only the header fits, no options.
  assert.equal(v(18, 5), 0)
  // height 36 → header + exactly one option row (18 + 18 == 36, not >).
  assert.equal(v(36, 5), 1)
  // height 35 → one row would need 36 > 35 → clipped to 0.
  assert.equal(v(35, 5), 0)
  // height 90 → header + 4 rows fit; capped by the option count, not height.
  assert.equal(v(90, 3), 3)
  assert.equal(v(900, 0), 0)
})

test('matrixLabel: defined label (even "") wins; missing falls back to the index', () => {
  const labels = new Map<number, string>([[0, 'A'], [1, '']])
  assert.equal(matrixLabel(labels, 0), 'A')
  assert.equal(matrixLabel(labels, 1), '') // empty string is a real label, not missing
  assert.equal(matrixLabel(labels, 2), '2') // missing → index as string
})
