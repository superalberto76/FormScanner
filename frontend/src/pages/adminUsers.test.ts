// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Unit tests for the admin user-list pure helpers. DOM/interaction
// (debounce timing, react-query paging) needs jsdom + a testing library, which
// are out of scope here (no new deps); the page-logic that IS decidable from
// data — pagination bounds, the "X–Y of N" summary, role/status rendering — is
// extracted into these pure helpers and exercised below.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  TEMP_PASSWORD_MIN_LENGTH,
  canActOnUser,
  canSubmitReset,
  hasNext,
  hasPrev,
  nameOrDash,
  pageSummary,
  passwordsMismatch,
  roleBadgeClass,
  statusBadgeClass,
  statusLabel,
  tempPasswordTooShort,
  userDisplayName,
} from './adminUsers.ts'

// ── status / role rendering ──────────────────────────────────────────────────
test('statusLabel maps disabled flag to the status label', () => {
  assert.equal(statusLabel(false), 'Active')
  assert.equal(statusLabel(true), 'Disabled')
})

test('statusBadgeClass is green when active, muted when disabled', () => {
  assert.equal(statusBadgeClass(false), 'badge-published')
  assert.equal(statusBadgeClass(true), 'badge-archived')
})

test('roleBadgeClass highlights ADMIN, mutes USER', () => {
  assert.equal(roleBadgeClass('ADMIN'), 'badge-role-owner')
  assert.equal(roleBadgeClass('USER'), 'badge-role-collaborator')
})

test('nameOrDash falls back to an em dash for null/blank', () => {
  assert.equal(nameOrDash('Mario'), 'Mario')
  assert.equal(nameOrDash('  Rossi '), 'Rossi')
  assert.equal(nameOrDash(null), '—')
  assert.equal(nameOrDash('   '), '—')
})

test('userDisplayName prefers full name, falls back to email', () => {
  assert.equal(
    userDisplayName({ first_name: 'Mario', last_name: 'Rossi', email: 'm@x.io' }),
    'Mario Rossi',
  )
  // Only one name present.
  assert.equal(
    userDisplayName({ first_name: 'Mario', last_name: null, email: 'm@x.io' }),
    'Mario',
  )
  // No names → email.
  assert.equal(
    userDisplayName({ first_name: null, last_name: '   ', email: 'm@x.io' }),
    'm@x.io',
  )
})

// ── row-action gating ────────────────────────────────────────────────────────
test('canActOnUser allows USER rows only (ADMIN rows get no actions)', () => {
  assert.equal(canActOnUser('USER'), true)
  assert.equal(canActOnUser('ADMIN'), false)
})

// ── reset-password validation (FE mirror of the BE min rule only) ─────────────
test('TEMP_PASSWORD_MIN_LENGTH matches the backend PASSWORD_MIN_LENGTH (8)', () => {
  assert.equal(TEMP_PASSWORD_MIN_LENGTH, 8)
})

test('tempPasswordTooShort only flags a non-empty, too-short password', () => {
  assert.equal(tempPasswordTooShort(''), false)      // empty → no hint yet
  assert.equal(tempPasswordTooShort('short'), true)  // 5 chars
  assert.equal(tempPasswordTooShort('longenough'), false)
})

test('passwordsMismatch only flags a non-empty diverging confirmation', () => {
  assert.equal(passwordsMismatch('abc12345', ''), false)       // confirm empty → no hint
  assert.equal(passwordsMismatch('abc12345', 'abc1234'), true) // diverges
  assert.equal(passwordsMismatch('abc12345', 'abc12345'), false)
})

test('canSubmitReset requires min length AND a matching confirmation', () => {
  assert.equal(canSubmitReset('abc12345', 'abc12345'), true)
  assert.equal(canSubmitReset('short', 'short'), false)        // too short, even if matching
  assert.equal(canSubmitReset('abc12345', 'different'), false) // long enough but mismatched
  assert.equal(canSubmitReset('', ''), false)                  // empty
})

// ── pagination math ──────────────────────────────────────────────────────────
test('pageSummary renders an inclusive 1-based range', () => {
  // First page, full.
  assert.equal(pageSummary(0, 20, 45), '1–20 of 45')
  // Middle page.
  assert.equal(pageSummary(20, 20, 45), '21–40 of 45')
  // Last, partial page uses the on-page count for the upper bound.
  assert.equal(pageSummary(40, 5, 45), '41–45 of 45')
})

test('pageSummary handles the empty result set', () => {
  assert.equal(pageSummary(0, 0, 0), '0 of 0')
})

test('hasPrev is true only past the first page', () => {
  assert.equal(hasPrev(0), false)
  assert.equal(hasPrev(20), true)
})

test('hasNext is true only while more rows remain', () => {
  // 45 total, 20/page: pages at offset 0 and 20 have a next, offset 40 does not.
  assert.equal(hasNext(0, 20, 45), true)
  assert.equal(hasNext(20, 20, 45), true)
  assert.equal(hasNext(40, 20, 45), false)
  // Exactly one full page → no next.
  assert.equal(hasNext(0, 20, 20), false)
  // Empty → no next.
  assert.equal(hasNext(0, 20, 0), false)
})
