// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Re-export of the centralized error helpers (canonical module: utils/apiError).
// Lets imports of '../api/errors' resolve to it.
export {
  normalizeApiError,
  describeError,
  isNotFound,
  isConflict,
  isAuthError,
  getStatus,
} from '../utils/apiError'
