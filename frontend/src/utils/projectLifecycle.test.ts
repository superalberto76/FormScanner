// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Unit tests for the project lifecycle graph. Runs on Node's built-in
// test runner with native TS type-stripping (see package.json "test" script).
// These assert the FE graph matches the backend _ALLOWED_TRANSITIONS exactly —
// the component renders exactly one button per allowed outgoing edge, so the
// per-state edge set IS what drives "renders N buttons matching the graph".
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  PROJECT_TRANSITIONS,
  TRANSITION_META,
  allowedTransitions,
} from './projectLifecycle.ts'

test('allowedTransitions returns the exact set per state (mirrors the BE graph)', () => {
  assert.deepEqual(allowedTransitions('DRAFT'), ['ACTIVE', 'CLOSED'])
  assert.deepEqual(allowedTransitions('ACTIVE'), ['CLOSED', 'DRAFT'])
  assert.deepEqual(allowedTransitions('CLOSED'), ['DRAFT', 'ARCHIVED'])
  assert.deepEqual(allowedTransitions('ARCHIVED'), [])
})

test('ARCHIVED is terminal — no outgoing edges (component renders nothing)', () => {
  assert.equal(allowedTransitions('ARCHIVED').length, 0)
})

test('no transition targets a state that is not in the graph', () => {
  const states = Object.keys(PROJECT_TRANSITIONS)
  for (const [, targets] of Object.entries(PROJECT_TRANSITIONS)) {
    for (const target of targets) {
      assert.ok(states.includes(target), `${target} is a known state`)
      assert.ok(TRANSITION_META[target], `${target} has button metadata`)
    }
  }
})

test('transition labels are the button copy', () => {
  assert.equal(TRANSITION_META.ACTIVE.label, 'Activate')
  assert.equal(TRANSITION_META.CLOSED.label, 'Close')
  assert.equal(TRANSITION_META.DRAFT.label, 'Reopen')
  assert.equal(TRANSITION_META.ARCHIVED.label, 'Archive')
})

test('only archiving is a danger move', () => {
  assert.equal(TRANSITION_META.ARCHIVED.danger, true)
  assert.equal(TRANSITION_META.ACTIVE.danger, false)
  assert.equal(TRANSITION_META.CLOSED.danger, false)
  assert.equal(TRANSITION_META.DRAFT.danger, false)
})
