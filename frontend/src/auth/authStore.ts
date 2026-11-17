// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState, useEffect } from 'react'
import type { UserRead } from '../api/types'

const TOKEN_KEY = 'fs_token'

// Module-level singletons — survive component re-mounts
let _token: string | null = localStorage.getItem(TOKEN_KEY)
let _user: UserRead | null = null
let _sessionExpired = false
const _listeners = new Set<() => void>()

function notify() {
  _listeners.forEach(fn => fn())
}

export function getToken(): string | null {
  return _token
}

export function getUser(): UserRead | null {
  return _user
}

export function setAuth(token: string, user: UserRead): void {
  _token = token
  _user = user
  localStorage.setItem(TOKEN_KEY, token)
  notify()
}

/**
 * Update the cached user profile without touching the token. Used to hydrate
 * `me` after a page refresh and to refresh the profile after a forced password
 * change (so the must_change_password gate releases).
 */
export function setUser(user: UserRead): void {
  _user = user
  notify()
}

export function clearAuth(): void {
  _token = null
  _user = null
  localStorage.removeItem(TOKEN_KEY)
  notify()
}

/**
 * Called when the API rejects a request with 401 on an authenticated route:
 * clears the token (so guarded routes redirect to /login) and records that the
 * redirect was due to an expired/invalid session so LoginPage can explain it.
 */
export function markSessionExpired(): void {
  if (!_token) return
  _sessionExpired = true
  clearAuth()
}

/** Reads and resets the session-expired flag (one-shot). */
export function consumeSessionExpired(): boolean {
  const v = _sessionExpired
  _sessionExpired = false
  return v
}

/** React hook — re-renders component whenever auth state changes. */
export function useAuth(): { token: string | null; user: UserRead | null } {
  const [state, setState] = useState({ token: getToken(), user: getUser() })

  useEffect(() => {
    const sync = () => setState({ token: getToken(), user: getUser() })
    _listeners.add(sync)
    return () => { _listeners.delete(sync) }
  }, [])

  return state
}
