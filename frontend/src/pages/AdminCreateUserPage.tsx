// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState, type FormEvent } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Layout } from '../components/Layout'
import { useToast } from '../components/ToastProvider'
import { api } from '../api/endpoints'
import { getStatus, normalizeApiError } from '../utils/apiError'
import type { UserCreateByAdmin, UserRead } from '../api/types'

const MIN_PW_LEN = 8

/**
 * Admin-only screen to create a non-admin user. The route is gated
 * to admins (AdminRoute) and the menu entry is admin-only, but the backend is
 * the authoritative gate (require_admin) — this UI only mirrors it. No way to
 * create an admin from here: the backend forces role=USER.
 */
export function AdminCreateUserPage() {
  const toast = useToast()

  const [firstName, setFirstName] = useState('')
  const [lastName, setLastName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [emailError, setEmailError] = useState('')

  const createM = useMutation<UserRead, unknown, UserCreateByAdmin>({
    mutationFn: (payload) => api.createUser(payload),
    onSuccess: (created) => {
      toast.success(`User ${created.email} created`)
      setFirstName(''); setLastName(''); setEmail(''); setPassword('')
      setEmailError('')
    },
    onError: (err) => {
      // 409 = duplicate email: surface it inline next to the email field.
      if (getStatus(err) === 409) {
        setEmailError('This email is already in use.')
      } else {
        toast.error(err)
      }
    },
  })

  const pwTooShort = password.length > 0 && password.length < MIN_PW_LEN
  const canSubmit =
    firstName.trim() !== '' &&
    lastName.trim() !== '' &&
    email.trim() !== '' &&
    password.length >= MIN_PW_LEN &&
    !createM.isPending

  function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (!canSubmit) return
    setEmailError('')
    createM.mutate({
      first_name: firstName.trim(),
      last_name: lastName.trim(),
      email: email.trim(),
      password,
    })
  }

  return (
    <Layout>
      <h1 className="mb-16">Create user</h1>
      <div className="card" style={{ maxWidth: 480 }}>
        <p className="info-msg mb-16">
          The new user signs in with their email and is required to change this
          password at first login.
        </p>
        <form onSubmit={handleSubmit} className="stack stack-12">
          <div className="form-group">
            <label htmlFor="u-first">First name</label>
            <input id="u-first" required value={firstName}
                   onChange={e => setFirstName(e.target.value)} />
          </div>
          <div className="form-group">
            <label htmlFor="u-last">Last name</label>
            <input id="u-last" required value={lastName}
                   onChange={e => setLastName(e.target.value)} />
          </div>
          <div className="form-group">
            <label htmlFor="u-email">Email</label>
            <input id="u-email" type="email" required value={email}
                   onChange={e => { setEmail(e.target.value); setEmailError('') }}
                   placeholder="user@example.com" />
            {emailError && <p className="error-msg">{emailError}</p>}
          </div>
          <div className="form-group">
            <label htmlFor="u-pw">Temporary password</label>
            <input id="u-pw" type="password" autoComplete="new-password" required
                   value={password} onChange={e => setPassword(e.target.value)} />
            {pwTooShort && (
              <p className="error-msg">Use at least {MIN_PW_LEN} characters.</p>
            )}
          </div>
          {createM.isError && getStatus(createM.error) !== 409 && (
            <p className="error-msg">{normalizeApiError(createM.error)}</p>
          )}
          <div className="row row-8">
            <button type="submit" className="btn btn-primary" disabled={!canSubmit}>
              {createM.isPending ? 'Creating…' : 'Create user'}
            </button>
          </div>
        </form>
      </div>
    </Layout>
  )
}
