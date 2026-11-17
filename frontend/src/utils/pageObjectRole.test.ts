// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Unit tests for the pure role helpers. Runs on Node's built-in
// test runner with native TS type-stripping — no extra dev dependency:
//   node --test 'src/**/*.test.ts'   (see package.json "test" script).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { holdsVariantIdentifier } from './pageObjectRole.ts'
import type { PageObjectRole, PageObjectRoleBinding } from '../api/domain.ts'

// Minimal role-binding factory — only the fields the helper reads matter.
const binding = (role: PageObjectRole, shared_group_id: string | null): PageObjectRoleBinding =>
  ({ role, shared_group_id } as PageObjectRoleBinding)

// ── holdsVariantIdentifier ────────────────────────────────────────────────────
test('true when the object itself holds VARIANT_IDENTIFIER (even with no bindings)', () => {
  assert.equal(holdsVariantIdentifier({ role: 'VARIANT_IDENTIFIER', shared_group_id: null }, []), true)
})

test('true when a shared sibling (same group) holds VARIANT_IDENTIFIER', () => {
  const bindings = [binding('VARIANT_IDENTIFIER', 'grp-1')]
  assert.equal(holdsVariantIdentifier({ role: null, shared_group_id: 'grp-1' }, bindings), true)
})

test('false when a VARIANT_IDENTIFIER binding is in a different group', () => {
  const bindings = [binding('VARIANT_IDENTIFIER', 'grp-2')]
  assert.equal(holdsVariantIdentifier({ role: null, shared_group_id: 'grp-1' }, bindings), false)
})

test('false when object has no shared_group_id and no own role (cannot borrow via sibling)', () => {
  const bindings = [binding('VARIANT_IDENTIFIER', 'grp-1')]
  assert.equal(holdsVariantIdentifier({ role: null, shared_group_id: null }, bindings), false)
})

test('false when the only shared sibling holds a different role', () => {
  const bindings = [binding('RESPONDENT_IDENTIFIER', 'grp-1')]
  assert.equal(holdsVariantIdentifier({ role: null, shared_group_id: 'grp-1' }, bindings), false)
})

test('does not treat two null shared_group_ids as the same group', () => {
  // A binding whose object is unshared (null group) must not match an unshared
  // object — null === null would be a false positive without the null guard.
  const bindings = [binding('VARIANT_IDENTIFIER', null)]
  assert.equal(holdsVariantIdentifier({ role: null, shared_group_id: null }, bindings), false)
})
