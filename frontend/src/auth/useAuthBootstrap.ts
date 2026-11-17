// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useState } from 'react'
import { api } from '../api/endpoints'
import { getToken, getUser, setUser, useAuth } from './authStore'

/**
 * Hydrate the cached user profile on app load.
 *
 * Only the token is persisted (localStorage); the user object lives in memory,
 * so after a hard refresh we have a token but no profile. Until we know the
 * profile we cannot evaluate the forced-password-change gate, so the app waits
 * for `me` to load. A 401 here clears the session via the shared client and the
 * app falls through to /login.
 *
 * Returns `true` once auth state is settled (no token, profile already present,
 * or the `me` fetch has resolved/failed).
 */
export function useAuthBootstrap(): boolean {
  const { token } = useAuth()
  // Settled already if there is no token, or the profile is in memory.
  const [ready, setReady] = useState(() => !getToken() || getUser() !== null)

  useEffect(() => {
    let cancelled = false
    if (token && getUser() === null) {
      api
        .me()
        .then((u) => { if (!cancelled) setUser(u) })
        .catch(() => { /* 401 clears the session in the API client */ })
        .finally(() => { if (!cancelled) setReady(true) })
    } else {
      setReady(true)
    }
    return () => { cancelled = true }
    // Re-run when the token changes (login / logout / session-expiry).
  }, [token])

  return ready
}
