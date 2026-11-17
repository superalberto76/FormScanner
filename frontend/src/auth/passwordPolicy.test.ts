// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Freeze + behaviour tests for the FE password policy. Runs on
// Node's built-in test runner with native TS type-stripping:
//   node --test 'src/**/*.test.ts'   (see package.json "test" script).
//
// ANTI-DIVERGENCE: the special-char set MUST stay byte-for-byte identical to the
// backend. The backend mirror is backend/app/core/security.py
// (PASSWORD_SPECIAL_CHARS) with its own freeze test in
// backend/tests/test_password_policy.py asserting the SAME literal below.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  PASSWORD_SPECIAL_CHARS,
  allChecksPass,
  isPasswordValid,
  passwordChecks,
} from './passwordPolicy.ts'

// The frozen literal — keep identical to the backend constant.
const FROZEN_SPECIAL = "@#$%^&*-_!+=[]{}|\\:',.?/`~\"()<>;"

const ok = (pw: string, confirm: string, id: string) =>
  passwordChecks(pw, confirm).find(c => c.id === id)?.ok

test('PASSWORD_SPECIAL_CHARS equals the frozen literal with no duplicates', () => {
  assert.equal(PASSWORD_SPECIAL_CHARS, FROZEN_SPECIAL)
  assert.equal(PASSWORD_SPECIAL_CHARS.length, 32)
  assert.equal(new Set(PASSWORD_SPECIAL_CHARS).size, PASSWORD_SPECIAL_CHARS.length)
})

test('passwordChecks lists the rules in display order', () => {
  const ids = passwordChecks('', '').map(c => c.id)
  assert.deepEqual(ids, ['min8', 'upper', 'lower', 'digit', 'special', 'match'])
})

test('a fully compliant password passes every rule', () => {
  const pw = 'Abcdef1@'
  assert.ok(isPasswordValid(pw))
  assert.ok(allChecksPass(pw, pw))
  for (const c of passwordChecks(pw, pw)) assert.ok(c.ok, `${c.id} should pass`)
})

test('each rule is flagged independently', () => {
  // too short (still has upper/lower/digit/special)
  assert.equal(ok('Ab1@', 'Ab1@', 'min8'), false)
  // no uppercase
  assert.equal(ok('abcdef1@', 'abcdef1@', 'upper'), false)
  // no lowercase
  assert.equal(ok('ABCDEF1@', 'ABCDEF1@', 'lower'), false)
  // no digit
  assert.equal(ok('Abcdefg@', 'Abcdefg@', 'digit'), false)
  // no special
  assert.equal(ok('Abcdefg1', 'Abcdefg1', 'special'), false)
})

test('every special character satisfies the special rule', () => {
  for (const ch of PASSWORD_SPECIAL_CHARS) {
    assert.equal(ok(`Abcdef1${ch}`, '', 'special'), true, `char ${ch} should count`)
  }
})

test('match reflects confirmation equality', () => {
  assert.equal(ok('Abcdef1@', 'Abcdef1@', 'match'), true)
  assert.equal(ok('Abcdef1@', 'different', 'match'), false)
  // both empty is not a match
  assert.equal(ok('', '', 'match'), false)
})

test('allChecksPass requires both validity and matching confirmation', () => {
  assert.ok(allChecksPass('Abcdef1@', 'Abcdef1@'))
  assert.ok(!allChecksPass('Abcdef1@', 'Abcdef1X')) // mismatch
  assert.ok(!allChecksPass('weak', 'weak')) // invalid policy
})
