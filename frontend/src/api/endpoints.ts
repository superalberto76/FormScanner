// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { apiFetch } from './client'
import type {
  PaginatedUsers,
  ThemeMode,
  UserCreateByAdmin,
  UserProfileUpdate,
  UserRead,
} from './types'
import { getToken } from '../auth/authStore'

const t = () => getToken()

export const api = {
  // ── Auth ───────────────────────────────────────────────────────────────────
  login: (email: string, password: string) =>
    apiFetch<{ access_token: string; token_type: string }>('/auth/login', {
      method: 'POST',
      json: { email, password },
    }),

  me: (token?: string) =>
    apiFetch<UserRead>('/auth/me', { token: token ?? t() }),

  // Self-service identity edit. first/last only; the backend schema
  // makes role/disabled/email unreachable. Returns the refreshed profile.
  updateProfile: (payload: UserProfileUpdate) =>
    apiFetch<UserRead>('/auth/me', { method: 'PATCH', json: payload, token: t() }),

  // Thin convenience over updateProfile for the Appearance screen: PATCH
  // only ui_theme. Returns the refreshed profile so the caller can setUser().
  updateTheme: (ui_theme: ThemeMode) => api.updateProfile({ ui_theme }),

  // Profile avatar. Reuses the IMAGE-object pipeline on the BE.
  // Upload/replace: multipart, field name 'file' (as the IMAGE upload). The BE
  // is the authoritative validator (real MIME, size, SVG sanitization).
  uploadMyAvatar: (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return apiFetch<UserRead>('/auth/me/avatar', { method: 'POST', formData: fd, token: t() })
  },
  // Remove the avatar; returns the refreshed profile (has_avatar=false).
  deleteMyAvatar: () =>
    apiFetch<UserRead>('/auth/me/avatar', { method: 'DELETE', token: t() }),
  // Fetch the served avatar binary as a Blob (authed); the FE turns it into an
  // object URL for an <img> — never injects SVG inline (no script execution).
  getMyAvatar: () =>
    apiFetch<Blob>('/auth/me/avatar', { token: t() }),

  // Forced first-login change and any later self-service change. Returns the
  // updated profile (must_change_password=false) so the caller can refresh auth.
  // The forced flow calls this with just the new password; the self-service
  // caller also passes the current password, which the
  // backend requires and verifies.
  changePassword: (newPassword: string, currentPassword?: string) =>
    apiFetch<UserRead>('/auth/change-password', {
      method: 'POST',
      json: {
        new_password: newPassword,
        ...(currentPassword ? { current_password: currentPassword } : {}),
      },
      token: t(),
    }),

  // ── Users (admin only) ──────────────────────────────────────────────────────
  createUser: (payload: UserCreateByAdmin) =>
    apiFetch<UserRead>('/users', { method: 'POST', json: payload, token: t() }),

  // Paginated + searchable user listing for the "Manage users" screen.
  // Admin-only on the BE (require_admin). The query string is built
  // from the provided params; a blank/absent `q` is omitted so it lists everyone.
  listUsers: (params: { q?: string; limit?: number; offset?: number } = {}) => {
    const qs = new URLSearchParams()
    if (params.q && params.q.trim()) qs.set('q', params.q.trim())
    if (params.limit != null) qs.set('limit', String(params.limit))
    if (params.offset != null) qs.set('offset', String(params.offset))
    const query = qs.toString()
    return apiFetch<PaginatedUsers>(`/users${query ? `?${query}` : ''}`, { token: t() })
  },

  // Enable/disable a USER account. Admin-only on the BE; ADMIN
  // targets are rejected (409). Returns the refreshed user.
  setUserDisabled: (userId: string, disabled: boolean) =>
    apiFetch<UserRead>(`/users/${userId}`, {
      method: 'PATCH',
      json: { disabled },
      token: t(),
    }),

  // Set a TEMPORARY password for a USER and force a change at next login.
  // Min-length-8 rule only — the rich policy applies at that next
  // login, NOT here. Admin-only; ADMIN targets are rejected (409).
  resetUserPassword: (userId: string, newPassword: string) =>
    apiFetch<UserRead>(`/users/${userId}/reset-password`, {
      method: 'POST',
      json: { new_password: newPassword },
      token: t(),
    }),

  // Fetch any user's served avatar as a Blob (authed; avatars are public to
  // logged-in users). Turned into an <img> object URL by
  // useUserAvatar — never injected inline (no SVG script execution).
  getUserAvatar: (userId: string) =>
    apiFetch<Blob>(`/users/${userId}/avatar`, { token: t() }),
}
