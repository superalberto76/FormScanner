// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState, type FormEvent } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'
import { api } from '../api/endpoints'
import { clearAuth, setUser, useAuth } from '../auth/authStore'
import { allChecksPass, passwordChecks } from '../auth/passwordPolicy'
import { normalizeApiError } from '../utils/apiError'

/**
 * Forced first-login password change. A user created by an admin
 * lands here (the ProtectedRoute gate redirects while must_change_password is
 * true) and cannot reach the rest of the app until the change succeeds.
 *
 * On success the backend returns the refreshed profile (must_change_password
 * false); we write it to the auth store, which releases the gate.
 */
export function ChangePasswordPage() {
  const { token, user } = useAuth()
  const navigate = useNavigate()

  const [pw, setPw] = useState('')
  const [confirm, setConfirm] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  // Not authenticated → login. Already cleared the flag → nothing to do here.
  if (!token) return <Navigate to="/login" replace />
  if (user && !user.must_change_password) return <Navigate to="/projects" replace />

  const checks = passwordChecks(pw, confirm)
  const canSubmit = allChecksPass(pw, confirm) && !loading

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (!canSubmit) return
    setError('')
    setLoading(true)
    try {
      const updated = await api.changePassword(pw)
      setUser(updated) // releases the forced-change gate
      navigate('/projects', { replace: true })
    } catch (err) {
      setError(normalizeApiError(err, 'Could not change the password.'))
    } finally {
      setLoading(false)
    }
  }

  function logout() {
    clearAuth()
    navigate('/login', { replace: true })
  }

  return (
    <div className="login-wrap">
      <div className="card login-card">
        <h1 className="login-title">Set a new password</h1>
        <p className="info-msg mb-16" style={{ color: 'var(--text)' }}>
          Your account uses a temporary password. Please choose a new one to continue.
        </p>
        <form onSubmit={handleSubmit} className="stack stack-16">
          <div className="form-group">
            <label htmlFor="new-pw">New password</label>
            <input
              id="new-pw"
              type="password"
              autoComplete="new-password"
              required
              value={pw}
              onChange={e => setPw(e.target.value)}
            />
          </div>
          <div className="form-group">
            <label htmlFor="confirm-pw">Confirm password</label>
            <input
              id="confirm-pw"
              type="password"
              autoComplete="new-password"
              required
              value={confirm}
              onChange={e => setConfirm(e.target.value)}
            />
          </div>
          <ul
            className="password-checklist"
            style={{ listStyle: 'none', padding: 0, margin: 0, fontSize: '0.85rem' }}
          >
            {checks.map(c => (
              <li
                key={c.id}
                style={{ color: c.ok ? 'var(--success)' : 'var(--text-muted)' }}
              >
                <span aria-hidden="true">{c.ok ? '✓' : '○'}</span> {c.label}
              </li>
            ))}
          </ul>
          {error && <p className="error-msg">{error}</p>}
          <button className="btn btn-primary" type="submit" disabled={!canSubmit}>
            {loading ? 'Saving…' : 'Save and continue'}
          </button>
          <button type="button" className="btn btn-secondary" onClick={logout} disabled={loading}>
            Log out
          </button>
        </form>
      </div>
    </div>
  )
}
