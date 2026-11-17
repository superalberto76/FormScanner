// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Pure helpers for the admin "Manage users" screen. Kept in a
// plain .ts module (no JSX) so they can be unit-tested with node:test — see
// adminUsers.test.ts. The wiring (debounce, query, table) lives in
// AdminUsersPage.tsx; everything decidable from data lives here.

/** Status label for a user row. */
export function statusLabel(disabled: boolean): string {
  return disabled ? 'Disabled' : 'Active'
}

/** Badge palette class for the status cell — green when active, muted when disabled. */
export function statusBadgeClass(disabled: boolean): string {
  return disabled ? 'badge-archived' : 'badge-published'
}

/** Badge palette class for the role cell — ADMIN highlighted, USER muted. */
export function roleBadgeClass(role: 'USER' | 'ADMIN'): string {
  return role === 'ADMIN' ? 'badge-role-owner' : 'badge-role-collaborator'
}

/** A name cell value, falling back to an em dash when null/blank. */
export function nameOrDash(value: string | null): string {
  return value?.trim() || '—'
}

/** Best human label for a user: "First Last" when present, else the email. */
export function userDisplayName(u: {
  first_name: string | null
  last_name: string | null
  email: string
}): string {
  const full = [u.first_name, u.last_name]
    .map(s => s?.trim())
    .filter(Boolean)
    .join(' ')
  return full || u.email
}

/**
 * "X–Y of N" pagination summary for the current page (1-based, inclusive).
 * *count* is the number of rows actually on this page, so the last (partial)
 * page reads correctly. Empty result → "0 of 0".
 */
export function pageSummary(offset: number, count: number, total: number): string {
  if (total === 0 || count === 0) return '0 of 0'
  const from = offset + 1
  const to = offset + count
  return `${from}–${to} of ${total}`
}

/** Whether the "Previous" control should be enabled. */
export function hasPrev(offset: number): boolean {
  return offset > 0
}

/** Whether the "Next" control should be enabled, given page size & total. */
export function hasNext(offset: number, limit: number, total: number): boolean {
  return offset + limit < total
}

// ── Row actions ───────────────────────────────────────────────────────────────
// Disable/enable + password reset apply to USERS only. The BE rejects ADMIN
// targets with 409; the row simply hides the controls. Mirrors the BE 409 guard.

/** Whether a row's admin actions (disable/reset) should render at all. */
export function canActOnUser(role: 'USER' | 'ADMIN'): boolean {
  return role === 'USER'
}

/**
 * Minimum length for an admin-assigned TEMPORARY password — the FE mirror of the
 * backend ``PASSWORD_MIN_LENGTH``. This is the min-length rule ONLY; the rich
 * policy (digit/upper/lower/special) is intentionally NOT mirrored here — it is
 * enforced when the user changes this temp password at their next login.
 */
export const TEMP_PASSWORD_MIN_LENGTH = 8

/** True once a non-empty password is below the minimum length (drives inline hint). */
export function tempPasswordTooShort(pw: string): boolean {
  return pw.length > 0 && pw.length < TEMP_PASSWORD_MIN_LENGTH
}

/** True once a non-empty confirmation diverges from the password (inline hint). */
export function passwordsMismatch(pw: string, confirm: string): boolean {
  return confirm.length > 0 && pw !== confirm
}

/** Whether the reset modal may submit: long enough AND the two fields match. */
export function canSubmitReset(pw: string, confirm: string): boolean {
  return pw.length >= TEMP_PASSWORD_MIN_LENGTH && pw === confirm
}
