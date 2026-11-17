// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Unit tests for the canvas preview selector. Runs on Node's built-in
// test runner with native TS type-stripping — no extra dev dependency:
//   node --test 'src/**/*.test.ts'   (see package.json "test" script).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { pickPreviewKind } from './objectPreview.ts'
import type { PageObjectType } from '../api/domain'

const ALL_TYPES: PageObjectType[] = ['TEXT', 'CODE', 'FREE_TEXT_AREA', 'QUESTION', 'MATRIX', 'IMAGE']

test('pickPreviewKind selects the matching preview for every visible type', () => {
  for (const object_type of ALL_TYPES) {
    assert.equal(pickPreviewKind({ visible: true, object_type }), object_type)
  }
})

test('pickPreviewKind renders nothing (null) when visible=false — geometry only', () => {
  for (const object_type of ALL_TYPES) {
    assert.equal(pickPreviewKind({ visible: false, object_type }), null)
  }
})

test('pickPreviewKind hides the IMAGE preview too when the object is not visible', () => {
  // visible=false → geometry only, INCLUDING the image thumbnail.
  assert.equal(pickPreviewKind({ visible: false, object_type: 'IMAGE' }), null)
})
