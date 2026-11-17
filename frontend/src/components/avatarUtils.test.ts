// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Unit tests for the Avatar pure helpers. Native TS type-stripping,
// no extra dev dependency:  node --test 'src/**/*.test.ts'  (package.json "test").
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { computeInitials, colorFromString } from './avatarUtils.ts'

// ── computeInitials ──────────────────────────────────────────────────────────
test('computeInitials: two-word full_name → first letter of each, uppercased', () => {
  assert.equal(computeInitials('Mario Rossi'), 'MR')
})

test('computeInitials: single-word name → one initial', () => {
  assert.equal(computeInitials('Mario'), 'M')
})

test('computeInitials: more than two words uses only the first two', () => {
  assert.equal(computeInitials('Mario Luigi Rossi'), 'ML')
})

test('computeInitials: trims and collapses surrounding/inner whitespace', () => {
  assert.equal(computeInitials('  mario   rossi '), 'MR')
})

test('computeInitials: blank name falls back to email local part (1–2 chars)', () => {
  assert.equal(computeInitials('', 'alex@example.com'), 'AL')
  assert.equal(computeInitials('   ', 'b@example.com'), 'B')
})

test('computeInitials: no name and no email → empty string', () => {
  assert.equal(computeInitials(''), '')
})

// ── colorFromString ──────────────────────────────────────────────────────────
test('colorFromString: deterministic and within the palette', () => {
  const a = colorFromString('Mario Rossi')
  const b = colorFromString('Mario Rossi')
  assert.equal(a, b)
  assert.match(a, /^#[0-9a-f]{6}$/)
})
