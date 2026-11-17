// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
// Single source of UI-theme logic.
//
// The persisted unit is the MODE (LIGHT/DARK/SYSTEM), never the resolved colour:
// SYSTEM follows the OS `prefers-color-scheme` and is resolved to light/dark at
// runtime. `applyResolved` flips `data-theme` on <html>, which activates the dark
// token overrides in index.css. The localStorage mirror (`fs_theme`) lets the
// inline boot script in index.html paint the right theme before first paint (no
// wrong-theme flash) and survives logout (the login page keeps the last theme).
//
// NOTE: keep the key + resolve logic in sync with the inline boot script in
// index.html (it cannot import this module — it must run before the bundle).
//
// All DOM access lives inside functions (no top-level `window`/`document`) so the
// module imports cleanly under the DOM-free `node --test` runner.
import type { ThemeMode } from '../api/types'

export type { ThemeMode }
export type Resolved = 'light' | 'dark'

export const THEME_KEY = 'fs_theme'

const SYSTEM_QUERY = '(prefers-color-scheme: dark)'

/** True when the OS currently prefers a dark colour scheme. */
export function prefersDark(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia(SYSTEM_QUERY).matches
  )
}

/** Resolve a stored MODE to the concrete light/dark theme to paint. */
export function resolve(mode: ThemeMode): Resolved {
  if (mode === 'SYSTEM') return prefersDark() ? 'dark' : 'light'
  return mode === 'DARK' ? 'dark' : 'light'
}

/** Apply the resolved theme by setting `data-theme` on the <html> element. */
export function applyResolved(r: Resolved): void {
  document.documentElement.dataset.theme = r
}

/** The MediaQueryList for the OS dark-mode preference (for live subscription). */
export function systemQuery(): MediaQueryList {
  return window.matchMedia(SYSTEM_QUERY)
}

/** Read the persisted MODE from localStorage; default SYSTEM. */
export function getStoredMode(): ThemeMode {
  try {
    const v = localStorage.getItem(THEME_KEY)
    if (v === 'LIGHT' || v === 'DARK' || v === 'SYSTEM') return v
  } catch {
    /* storage disabled / private mode — fall through to the default */
  }
  return 'SYSTEM'
}

/** Persist the MODE to localStorage (mirrors the value the boot script reads). */
export function setStoredMode(mode: ThemeMode): void {
  try {
    localStorage.setItem(THEME_KEY, mode)
  } catch {
    /* storage disabled — the in-memory ThemeController still applies the theme */
  }
}
