// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Pure helpers for the self-service Profile page. Kept in a plain
// .ts module (no JSX) so they can be unit-tested with Node's native test runner
// (`node --test`) without rendering React — see profile.test.ts. Deliberately
// free of app-runtime imports (only `import type`), so the test runner never
// pulls in the API/client layer. The component (ProfilePage.tsx) holds only
// wiring; the rules live here.
import type { UserProfileUpdate, UserRead } from '../api/types'

/**
 * Display name for the avatar/header: "First Last" from the profile, falling
 * back to whichever part is present, then to the email. Mirrors the initials
 * source used by the Avatar component.
 */
export function profileDisplayName(user: UserRead): string {
  const name = [user.first_name, user.last_name]
    .map(p => p?.trim() ?? '')
    .filter(Boolean)
    .join(' ')
  return name || user.email
}

/**
 * True when the edited names differ (trimmed) from what the profile currently
 * holds — i.e. there is something to save.
 */
export function identityChanged(user: UserRead, firstName: string, lastName: string): boolean {
  return (
    firstName.trim() !== (user.first_name ?? '').trim() ||
    lastName.trim() !== (user.last_name ?? '').trim()
  )
}

/**
 * Whether the identity "Save" button should be enabled: not pending, both
 * fields non-blank, and at least one value actually changed.
 */
export function canSaveIdentity(
  user: UserRead,
  firstName: string,
  lastName: string,
  pending: boolean,
): boolean {
  if (pending) return false
  if (firstName.trim() === '' || lastName.trim() === '') return false
  return identityChanged(user, firstName, lastName)
}

/** Build the PATCH /auth/me payload (trimmed). Only called when both are non-blank. */
export function buildIdentityPayload(firstName: string, lastName: string): UserProfileUpdate {
  return { first_name: firstName.trim(), last_name: lastName.trim() }
}

/**
 * Client-side pre-check for an avatar file before upload.
 * The backend is the authoritative validator (real-MIME sniff, SVG sanitize);
 * this just avoids an obviously-doomed round-trip. Returns an error
 * string, or null when the file passes the basic type/size gate. An empty
 * `file.type` (some browsers) is allowed through so the BE can decide.
 *
 * `accept`/`maxBytes` are passed in (the caller sources them from
 * `IMAGE_ACCEPT`/`IMAGE_MAX_BYTES` in api/domain) so this module stays free
 * of API-layer imports and remains unit-testable under `node --test`.
 */
export function avatarPreCheckError(
  file: File,
  accept: string,
  maxBytes: number,
): string | null {
  const allowed = accept.split(',')
  if (file.type && !allowed.includes(file.type)) {
    return 'Unsupported format. Use PNG, JPEG, BMP or SVG.'
  }
  if (file.size > maxBytes) {
    const mb = Math.round(maxBytes / (1024 * 1024))
    return `Image too large (max ${mb} MB).`
  }
  return null
}
