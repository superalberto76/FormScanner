// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// TypeScript interfaces matching backend Pydantic schemas exactly.
//
// This file carries the shared identity types (UserRead) used by auth and user
// administration; the domain types live in api/domain.ts.

/** UI theme mode. The MODE is stored; SYSTEM is resolved at runtime. */
export type ThemeMode = 'LIGHT' | 'DARK' | 'SYSTEM'

export interface UserRead {
  id: string
  email: string
  full_name: string | null
  first_name: string | null
  last_name: string | null
  role: 'USER' | 'ADMIN'
  disabled: boolean
  must_change_password: boolean
  // Persisted UI theme preference; SYSTEM follows the OS at runtime.
  ui_theme: ThemeMode
  // Whether a profile photo has been uploaded. The internal
  // avatar_path is never exposed; the FE fetches the bytes via GET /auth/me/avatar.
  has_avatar: boolean
  created_at: string
  updated_at: string
  last_login_at: string | null
}

/** One row in the admin user list — GET /users. Mirrors BE UserListItem. */
export interface UserListItem {
  id: string
  email: string
  first_name: string | null
  last_name: string | null
  role: 'USER' | 'ADMIN'
  disabled: boolean
  has_avatar: boolean
}

/** Envelope for GET /users — one page plus paging totals. Mirrors BE PaginatedUsers. */
export interface PaginatedUsers {
  items: UserListItem[]
  total: number
  limit: number
  offset: number
}

/** Payload for POST /users — admin creates a non-admin user. */
export interface UserCreateByAdmin {
  first_name: string
  last_name: string
  email: string
  password: string
}

/**
 * Payload for PATCH /auth/me — self-service identity edit.
 * first/last only: a self-service user can never change role/disabled/email.
 */
export interface UserProfileUpdate {
  first_name?: string
  last_name?: string
  // The only non-name field a self-service user may set.
  ui_theme?: ThemeMode
}
