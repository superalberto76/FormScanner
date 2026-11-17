// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { ApiError } from '../api/client'

/**
 * Centralized API-error normalization.
 *
 * The backend already returns a meaningful `detail` for domain errors (409/422
 * especially — published-version conflicts, duplicate marks/cells, missing
 * config, validation messages). This module wraps those with status-aware,
 * user-friendly framing so the whole app speaks with one voice and never fails
 * silently.
 */
export function getStatus(err: unknown): number | null {
  return err instanceof ApiError ? err.status : null
}

/**
 * The machine-readable code of a STRUCTURED API error (e.g. `LOCK_LOST`), or
 * null for any other error. Branch on this, never on the message text: the
 * message is for humans and may be reworded.
 */
export function getErrorCode(err: unknown): string | null {
  return err instanceof ApiError ? err.code ?? null : null
}

export function normalizeApiError(err: unknown, fallback = 'Something went wrong'): string {
  if (err instanceof ApiError) {
    const detail = err.detail?.trim()
    switch (err.status) {
      case 401:
        return 'Your session has expired or is invalid. Please log in again.'
      case 403:
        return detail ? `Not allowed: ${detail}` : 'You don’t have permission to do that.'
      case 404:
        return detail ? `Not found: ${detail}` : 'That item no longer exists.'
      case 409:
        // Conflicts carry the most useful backend message (e.g. "Cannot publish:
        // version has no code regions", "Version is not DRAFT", duplicate mark).
        return detail || 'That action conflicts with the current state.'
      case 422:
        return detail ? `Invalid input: ${detail}` : 'Some input was invalid.'
      default:
        return detail || fallback
    }
  }
  if (err instanceof Error && err.message) return err.message
  return fallback
}

/** Alias used by inline form/dialog error displays. */
export const describeError = normalizeApiError

export function isNotFound(err: unknown): boolean {
  return getStatus(err) === 404
}

export function isConflict(err: unknown): boolean {
  return getStatus(err) === 409
}

export function isAuthError(err: unknown): boolean {
  const s = getStatus(err)
  return s === 401 || s === 403
}
