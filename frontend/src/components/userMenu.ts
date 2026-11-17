// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Pure helpers for the user menu. Kept in a plain .ts module (no JSX)
// so they can be unit-tested with Node's native test runner — see userMenu.test.ts.
import type { UserRead } from '../api/types'

/** The label shown for the user: full name when set, otherwise the email. */
export function getDisplayName(user: UserRead): string {
  return user.full_name?.trim() || user.email
}

/**
 * Whether the "Add user" entry should appear. Front-end gating only — the backend
 * `require_admin` check stays authoritative for the actual route/endpoint.
 */
export function canAddUser(user: UserRead): boolean {
  return user.role === 'ADMIN'
}

/**
 * Whether the "Manage users" entry (admin user-management screen)
 * should appear. Front-end gating only — the backend `require_admin` on
 * GET /users and the `AdminRoute` guard stay authoritative.
 */
export function canManageUsers(user: UserRead): boolean {
  return user.role === 'ADMIN'
}
