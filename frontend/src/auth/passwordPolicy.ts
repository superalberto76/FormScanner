// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Frontend mirror of the backend password policy.
 *
 * SINGLE SOURCE OF TRUTH lives on the backend in
 * `backend/app/core/security.py` (PASSWORD_SPECIAL_CHARS + password_policy_errors).
 * The special-character set below MUST stay byte-for-byte identical to the BE
 * string; freeze tests on both sides assert against the same frozen literal:
 *   frontend/src/auth/passwordPolicy.test.ts
 *   backend/tests/test_password_policy.py
 * Do not edit one side without the other.
 */

export const PASSWORD_MIN_LENGTH = 8

// Allowed special characters (ASCII). Note plain ' and " are members; the
// backslash is escaped once. Membership is tested as a Set to avoid the
// escaping bugs a regex character class would invite. Same chars as the BE.
export const PASSWORD_SPECIAL_CHARS = "@#$%^&*-_!+=[]{}|\\:',.?/`~\"()<>;"

const SPECIAL = new Set(PASSWORD_SPECIAL_CHARS)

export interface PasswordCheck {
  id: string
  label: string
  ok: boolean
}

/** The policy rules that depend only on the password itself (no confirmation). */
function ruleChecks(pw: string): PasswordCheck[] {
  return [
    { id: 'min8', label: 'At least 8 characters', ok: pw.length >= PASSWORD_MIN_LENGTH },
    { id: 'upper', label: 'One uppercase letter', ok: /[A-Z]/.test(pw) },
    { id: 'lower', label: 'One lowercase letter', ok: /[a-z]/.test(pw) },
    { id: 'digit', label: 'One digit', ok: /[0-9]/.test(pw) },
    { id: 'special', label: 'One special character', ok: [...pw].some(c => SPECIAL.has(c)) },
  ]
}

/**
 * Full checklist for the UI, in display order. The trailing `match` row
 * covers confirmation equality and is FE-only (the BE has no concept of it).
 */
export function passwordChecks(pw: string, confirm: string): PasswordCheck[] {
  return [
    ...ruleChecks(pw),
    { id: 'match', label: 'The two passwords match', ok: pw.length > 0 && pw === confirm },
  ]
}

/** True when *pw* satisfies every policy rule (mirrors BE; ignores confirmation). */
export function isPasswordValid(pw: string): boolean {
  return ruleChecks(pw).every(c => c.ok)
}

/** True when the password is valid AND the confirmation matches it. */
export function allChecksPass(pw: string, confirm: string): boolean {
  return isPasswordValid(pw) && pw === confirm
}
