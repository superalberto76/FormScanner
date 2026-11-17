// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  profileDisplayName,
  identityChanged,
  canSaveIdentity,
  buildIdentityPayload,
  avatarPreCheckError,
} from './profile.ts'
import type { UserRead } from '../api/types.ts'

// Mirrors api/domain's IMAGE_ACCEPT / IMAGE_MAX_BYTES (the values the page
// passes in). Kept local so this pure test never imports the API/client layer.
const ACCEPT = 'image/png,image/jpeg,image/bmp,image/svg+xml'
const MAX_BYTES = 10 * 1024 * 1024

// Duck-typed File: avatarPreCheckError only reads .type and .size, so a literal
// avoids allocating a real multi-MB blob to exercise the size branch.
function fakeFile(type: string, size: number): File {
  return { type, size } as unknown as File
}

function makeUser(over: Partial<UserRead> = {}): UserRead {
  return {
    id: 'u1',
    email: 'mario.rossi@example.com',
    full_name: null,
    first_name: 'Mario',
    last_name: 'Rossi',
    role: 'USER',
    disabled: false,
    must_change_password: false,
    ui_theme: 'SYSTEM',
    has_avatar: false,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    last_login_at: null,
    ...over,
  }
}

// ── profileDisplayName ───────────────────────────────────────────────────────
test('profileDisplayName joins first + last', () => {
  assert.equal(profileDisplayName(makeUser()), 'Mario Rossi')
})

test('profileDisplayName uses whichever part is present', () => {
  assert.equal(profileDisplayName(makeUser({ last_name: null })), 'Mario')
})

test('profileDisplayName falls back to email when no name', () => {
  const u = makeUser({ first_name: null, last_name: null })
  assert.equal(profileDisplayName(u), 'mario.rossi@example.com')
})

// ── identityChanged ──────────────────────────────────────────────────────────
test('identityChanged is false when nothing changed', () => {
  assert.equal(identityChanged(makeUser(), 'Mario', 'Rossi'), false)
})

test('identityChanged ignores surrounding whitespace', () => {
  assert.equal(identityChanged(makeUser(), '  Mario ', 'Rossi '), false)
})

test('identityChanged is true when a name differs', () => {
  assert.equal(identityChanged(makeUser(), 'Luigi', 'Rossi'), true)
})

test('identityChanged treats null current name as empty', () => {
  const u = makeUser({ first_name: null })
  assert.equal(identityChanged(u, '', 'Rossi'), false)
  assert.equal(identityChanged(u, 'Mario', 'Rossi'), true)
})

// ── canSaveIdentity ──────────────────────────────────────────────────────────
test('canSaveIdentity false while pending', () => {
  assert.equal(canSaveIdentity(makeUser(), 'Luigi', 'Rossi', true), false)
})

test('canSaveIdentity false when a field is blank', () => {
  assert.equal(canSaveIdentity(makeUser(), '', 'Rossi', false), false)
  assert.equal(canSaveIdentity(makeUser(), 'Mario', '   ', false), false)
})

test('canSaveIdentity false when unchanged', () => {
  assert.equal(canSaveIdentity(makeUser(), 'Mario', 'Rossi', false), false)
})

test('canSaveIdentity true when changed, non-blank, not pending', () => {
  assert.equal(canSaveIdentity(makeUser(), 'Luigi', 'Rossi', false), true)
})

// ── buildIdentityPayload ─────────────────────────────────────────────────────
test('buildIdentityPayload trims both fields', () => {
  assert.deepEqual(buildIdentityPayload('  Luigi ', ' Verdi '), {
    first_name: 'Luigi',
    last_name: 'Verdi',
  })
})

// ── avatarPreCheckError ──────────────────────────────────────────────────────
test('avatarPreCheckError accepts a valid PNG', () => {
  assert.equal(avatarPreCheckError(fakeFile('image/png', 1024), ACCEPT, MAX_BYTES), null)
})

test('avatarPreCheckError accepts an SVG', () => {
  assert.equal(avatarPreCheckError(fakeFile('image/svg+xml', 1024), ACCEPT, MAX_BYTES), null)
})

test('avatarPreCheckError rejects an unsupported type', () => {
  const err = avatarPreCheckError(fakeFile('image/gif', 1024), ACCEPT, MAX_BYTES)
  assert.match(err ?? '', /Unsupported format/)
})

test('avatarPreCheckError rejects an oversize file', () => {
  const err = avatarPreCheckError(fakeFile('image/png', MAX_BYTES + 1), ACCEPT, MAX_BYTES)
  assert.match(err ?? '', /too large/)
})

test('avatarPreCheckError allows an empty type (BE decides)', () => {
  assert.equal(avatarPreCheckError(fakeFile('', 1024), ACCEPT, MAX_BYTES), null)
})
