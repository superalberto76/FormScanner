// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Pure helpers for the Avatar badge. Kept dependency-free and in a
// plain .ts module so they can be unit-tested with Node's native test runner
// (`node --test`) without rendering React — see avatarUtils.test.ts.

/**
 * Derive up to two uppercase initials for an avatar badge.
 * - From `name`: the first letter of each of up to two whitespace-separated words.
 * - If `name` is blank, fall back to the first 1–2 chars of the email local part
 *   (the part before `@`).
 * - If neither yields anything, returns an empty string (Avatar still renders).
 */
export function computeInitials(name: string, email?: string): string {
  const words = (name ?? '').trim().split(/\s+/).filter(Boolean)
  if (words.length > 0) {
    return words
      .slice(0, 2)
      .map(w => w[0])
      .join('')
      .toUpperCase()
  }
  const local = (email ?? '').split('@')[0] ?? ''
  return local.slice(0, 2).toUpperCase()
}

// A small fixed palette so badge colours stay on-brand and legible against white
// text. Reused for every initials badge; photos bypass this entirely.
const AVATAR_COLORS = [
  '#2563eb', // primary blue
  '#7c3aed', // violet
  '#0891b2', // cyan
  '#059669', // emerald
  '#d97706', // amber
  '#dc2626', // red
  '#db2777', // pink
]

/**
 * Deterministic background colour from a string, so the same user always gets the
 * same badge colour. Simple polynomial hash — no crypto, no deps.
 */
export function colorFromString(str: string): string {
  let hash = 0
  for (let i = 0; i < str.length; i++) {
    hash = (hash * 31 + str.charCodeAt(i)) | 0
  }
  return AVATAR_COLORS[Math.abs(hash) % AVATAR_COLORS.length]
}
