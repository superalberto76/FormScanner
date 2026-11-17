// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Unit tests for the user-menu pure helpers. DOM/interaction tests
// (Escape, click-outside, focus management) would need jsdom + a testing library,
// which are out of scope here (no new deps); the testable logic is extracted into
// these pure helpers and the wiring lives in UserMenu.tsx.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import type { UserRead } from '../api/types.ts'
import { getDisplayName, canAddUser, canManageUsers } from './userMenu.ts'

function makeUser(over: Partial<UserRead> = {}): UserRead {
  return {
    id: '1',
    email: 'user@example.com',
    full_name: null,
    first_name: null,
    last_name: null,
    role: 'USER',
    disabled: false,
    must_change_password: false,
    ui_theme: 'SYSTEM',
    has_avatar: false,
    created_at: '',
    updated_at: '',
    last_login_at: null,
    ...over,
  }
}

// ── getDisplayName ───────────────────────────────────────────────────────────
test('getDisplayName prefers full_name when set', () => {
  assert.equal(getDisplayName(makeUser({ full_name: 'Mario Rossi' })), 'Mario Rossi')
})

test('getDisplayName falls back to email when full_name is null or blank', () => {
  assert.equal(getDisplayName(makeUser({ full_name: null })), 'user@example.com')
  assert.equal(getDisplayName(makeUser({ full_name: '   ' })), 'user@example.com')
})

// ── canAddUser ───────────────────────────────────────────────────────────────
test('canAddUser is true only for ADMIN', () => {
  assert.equal(canAddUser(makeUser({ role: 'ADMIN' })), true)
  assert.equal(canAddUser(makeUser({ role: 'USER' })), false)
})

// ── canManageUsers ───────────────────────────────────────────────────────────
test('canManageUsers is true only for ADMIN', () => {
  assert.equal(canManageUsers(makeUser({ role: 'ADMIN' })), true)
  assert.equal(canManageUsers(makeUser({ role: 'USER' })), false)
})
