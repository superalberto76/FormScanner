// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useState } from 'react'
import { Navigate } from 'react-router-dom'
import { useMutation } from '@tanstack/react-query'
import { Layout } from '../components/Layout'
import { useToast } from '../components/ToastProvider'
import { api } from '../api/endpoints'
import { setUser, useAuth } from '../auth/authStore'
import type { ThemeMode, UserRead } from '../api/types'
import { applyResolved, resolve, systemQuery, type Resolved } from '../theme/theme'

/**
 * "Appearance" screen: choose the UI theme — Sync with system /
 * Light / Dark. The choice is persisted to the DB (PATCH /auth/me) so it follows
 * the user across devices; ThemeController applies it app-wide. Two preview cards
 * show what light/dark look like, with an "Active" badge on the one currently in
 * use (in SYSTEM it follows the OS live).
 */
export function AppearancePage() {
  const { token, user } = useAuth()

  if (!token) return <Navigate to="/login" replace />
  if (!user) {
    return (
      <Layout>
        <p className="info-msg">Loading…</p>
      </Layout>
    )
  }

  return <AppearanceView user={user} />
}

const MODES: { value: ThemeMode; label: string }[] = [
  { value: 'SYSTEM', label: 'Sync with system' },
  { value: 'LIGHT', label: 'Light' },
  { value: 'DARK', label: 'Dark' },
]

/**
 * The Resolved theme for a given MODE, re-computed live: when the mode is SYSTEM
 * it subscribes to `prefers-color-scheme` so the "Active" badge follows the OS in
 * real time (no reload). LIGHT/DARK are fixed and need no subscription.
 */
function useResolvedTheme(mode: ThemeMode): Resolved {
  const [resolved, setResolved] = useState<Resolved>(() => resolve(mode))

  useEffect(() => {
    setResolved(resolve(mode))
    if (mode !== 'SYSTEM') return
    const mq = systemQuery()
    const onChange = () => setResolved(resolve('SYSTEM'))
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [mode])

  return resolved
}

function AppearanceView({ user }: { user: UserRead }) {
  const toast = useToast()

  // Local mirror of the chosen mode for instant UI feedback; re-seeded if the
  // cached profile changes underneath us (another tab/device).
  const [mode, setMode] = useState<ThemeMode>(user.ui_theme)
  useEffect(() => setMode(user.ui_theme), [user.ui_theme])

  const resolved = useResolvedTheme(mode)

  const themeM = useMutation<UserRead, unknown, ThemeMode>({
    mutationFn: (next) => api.updateTheme(next),
    onSuccess: (updated) => {
      setUser(updated) // ThemeController reconciles + applies from the fresh profile
      toast.success('Theme updated')
    },
    onError: (err) => {
      // Revert the optimistic state + applied theme to the last persisted choice.
      setMode(user.ui_theme)
      applyResolved(resolve(user.ui_theme))
      toast.error(err)
    },
  })

  function handlePick(next: ThemeMode) {
    if (next === mode) return
    setMode(next)
    applyResolved(resolve(next)) // optimistic: switch instantly, before the PATCH
    themeM.mutate(next)
  }

  return (
    <Layout>
      <h1 className="mb-16">Appearance</h1>

      <div className="card mb-16" style={{ maxWidth: 560 }}>
        <div className="form-group">
          <label htmlFor="theme-mode">Theme mode</label>
          <select
            id="theme-mode"
            value={mode}
            disabled={themeM.isPending}
            onChange={(e) => handlePick(e.target.value as ThemeMode)}
          >
            {MODES.map((m) => (
              <option key={m.value} value={m.value}>{m.label}</option>
            ))}
          </select>
          <p className="info-msg">
            With “Sync with system” the theme follows your device settings and
            updates in real time.
          </p>
        </div>

        <div className="row row-16 mt-16" style={{ alignItems: 'stretch', flexWrap: 'wrap' }}>
          <ThemePreview variant="light" label="Light theme" active={resolved === 'light'} />
          <ThemePreview variant="dark" label="Dark theme" active={resolved === 'dark'} />
        </div>
      </div>
    </Layout>
  )
}

// Fixed light/dark palettes for the static mini-mockups. These are DELIBERATELY
// literal (not the themeable tokens): each card must always show its own theme,
// independent of the theme currently applied to the app. Mirror the :root light
// values and the :root[data-theme="dark"] overrides in index.css.
const PREVIEW_PALETTE = {
  light: { bg: '#f5f7fa', surface: '#ffffff', border: '#e2e8f0', text: '#111827', muted: '#6b7280', primary: '#2563eb' },
  dark: { bg: '#0f172a', surface: '#1e293b', border: '#334155', text: '#e2e8f0', muted: '#94a3b8', primary: '#3b82f6' },
} as const

/** A static mini-mockup of the app rendered in a fixed light/dark palette. */
function ThemePreview({
  variant,
  label,
  active,
}: {
  variant: 'light' | 'dark'
  label: string
  active: boolean
}) {
  const p = PREVIEW_PALETTE[variant]
  return (
    <div
      style={{
        flex: '1 1 200px',
        minWidth: 200,
        border: `1px solid ${p.border}`,
        borderRadius: 'var(--radius)',
        overflow: 'hidden',
        background: p.bg,
        outline: active ? '2px solid var(--primary)' : 'none',
        outlineOffset: 2,
      }}
    >
      {/* faux header */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '8px 10px',
          background: p.surface,
          borderBottom: `1px solid ${p.border}`,
        }}
      >
        <span style={{ fontWeight: 700, color: p.primary, fontSize: '0.8rem' }}>FormScanner</span>
        <span style={{ marginLeft: 'auto', width: 18, height: 18, borderRadius: '50%', background: p.primary }} />
      </div>

      {/* faux body */}
      <div style={{ padding: 12 }}>
        <div
          style={{
            background: p.surface,
            border: `1px solid ${p.border}`,
            borderRadius: 'var(--radius)',
            padding: 10,
          }}
        >
          <div style={{ height: 8, width: '60%', borderRadius: 4, background: p.text, opacity: 0.85 }} />
          <div style={{ height: 6, width: '85%', borderRadius: 4, background: p.muted, marginTop: 8 }} />
          <div style={{ height: 6, width: '70%', borderRadius: 4, background: p.muted, marginTop: 6 }} />
          <span
            style={{
              display: 'inline-block',
              marginTop: 12,
              padding: '4px 12px',
              borderRadius: 'var(--radius)',
              background: p.primary,
              color: '#fff',
              fontSize: '0.72rem',
              fontWeight: 600,
            }}
          >
            Action
          </span>
        </div>
      </div>

      {/* footer: title + Active badge */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '8px 10px',
          background: p.surface,
          borderTop: `1px solid ${p.border}`,
        }}
      >
        <span style={{ color: p.text, fontSize: '0.8rem', fontWeight: 600 }}>{label}</span>
        {active && (
          <span
            style={{
              padding: '2px 8px',
              borderRadius: 9999,
              background: 'var(--primary)',
              color: '#fff',
              fontSize: '0.68rem',
              fontWeight: 700,
              textTransform: 'uppercase',
              letterSpacing: '0.03em',
            }}
          >
            Active
          </span>
        )}
      </div>
    </div>
  )
}
