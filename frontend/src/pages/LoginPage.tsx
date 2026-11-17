// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState, useMemo, type FormEvent } from 'react'
import { useNavigate, Navigate } from 'react-router-dom'
import { apiFetch } from '../api/client'
import { api } from '../api/endpoints'
import { setAuth, useAuth, consumeSessionExpired } from '../auth/authStore'
import { normalizeApiError } from '../utils/apiError'
import type { UserRead } from '../api/types'

export function LoginPage() {
  const { token } = useAuth()
  const navigate = useNavigate()

  // One-shot: was this login screen reached because the session expired?
  const sessionExpired = useMemo(() => consumeSessionExpired(), [])

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  // Already logged in — redirect (declaratively, no navigate-during-render)
  if (token) return <Navigate to="/projects" replace />

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const { access_token } = await api.login(email, password)
      // Fetch user profile with the new token before persisting auth
      const user = await apiFetch<UserRead>('/auth/me', { token: access_token })
      setAuth(access_token, user)
      navigate('/projects', { replace: true })
    } catch (err) {
      setError(normalizeApiError(err, 'Connection error — is the backend reachable?'))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="login-wrap">
      <div className="card login-card">
        <h1 className="login-title">FormScanner</h1>
        {sessionExpired && (
          <p className="info-msg mb-16" style={{ color: 'var(--text)' }}>
            Your session expired. Please log in again.
          </p>
        )}
        <form onSubmit={handleSubmit} className="stack stack-16">
          <div className="form-group">
            <label htmlFor="email">Email</label>
            <input
              id="email"
              type="email"
              autoComplete="email"
              required
              value={email}
              onChange={e => setEmail(e.target.value)}
              placeholder="admin@example.com"
            />
          </div>
          <div className="form-group">
            <label htmlFor="password">Password</label>
            <input
              id="password"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={e => setPassword(e.target.value)}
            />
          </div>
          {error && <p className="error-msg">{error}</p>}
          <button className="btn btn-primary" type="submit" disabled={loading}>
            {loading ? 'Logging in…' : 'Login'}
          </button>
        </form>
      </div>
    </div>
  )
}
