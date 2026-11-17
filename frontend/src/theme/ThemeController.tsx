// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect } from 'react'
import { useAuth } from '../auth/authStore'
import { applyResolved, getStoredMode, resolve, setStoredMode, systemQuery } from './theme'

/**
 * Headless theme reconciler. Mounted once, inside the providers and
 * above <App/>, so it runs for the whole app lifetime — including App's loading
 * gate, so the theme is right even before routing settles.
 *
 * Source of truth for the MODE: the logged-in user's `ui_theme` (so the choice
 * follows them across devices), falling back to the localStorage mirror while the
 * profile is still hydrating or when logged out (keeps the login page on the last
 * theme — clearAuth deliberately leaves `fs_theme` untouched).
 *
 * On every effective-mode change it persists the mirror and applies the resolved
 * theme. It also subscribes to the OS `prefers-color-scheme` for its whole
 * lifetime and, ONLY when the effective mode is SYSTEM, re-applies on an OS
 * day/night switch — live, with no reload.
 */
export function ThemeController(): null {
  const { user } = useAuth()
  const mode = user?.ui_theme ?? getStoredMode()

  // Apply + mirror whenever the effective mode changes (login, refresh, change).
  useEffect(() => {
    setStoredMode(mode)
    applyResolved(resolve(mode))
  }, [mode])

  // Live OS theme change: re-resolve only when following the system (SYSTEM).
  useEffect(() => {
    const mq = systemQuery()
    const onChange = () => {
      if (mode === 'SYSTEM') applyResolved(resolve('SYSTEM'))
    }
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [mode])

  return null
}
