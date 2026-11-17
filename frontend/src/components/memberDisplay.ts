// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Pure helpers for rendering project members. Kept dependency-free
// in a plain .ts module so the formatting stays consistent across the avatar
// stack, the members modal, and the add-search results.
import type { ProjectRole } from '../api/domain'

/**
 * Display name from first/last name. Returns '' when both are blank — callers
 * decide the fallback (the Avatar still renders initials from email when given).
 */
export function memberDisplayName(
  firstName: string | null,
  lastName: string | null,
): string {
  return [firstName, lastName].filter(Boolean).join(' ').trim()
}

/** Label for a member's role badge. */
export function roleLabel(role: ProjectRole): string {
  return role === 'OWNER' ? 'Owner' : 'Collaborator'
}
