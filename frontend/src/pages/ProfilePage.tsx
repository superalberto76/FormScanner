// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useRef, useState, type ChangeEvent, type FormEvent } from 'react'
import { Navigate } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Layout } from '../components/Layout'
import { Avatar } from '../components/Avatar'
import { useToast } from '../components/ToastProvider'
import { useAvatarImage } from '../hooks/useAvatarImage'
import { api } from '../api/endpoints'
import { IMAGE_ACCEPT, IMAGE_MAX_BYTES } from '../api/domain'
import { setUser, useAuth } from '../auth/authStore'
import { allChecksPass, passwordChecks } from '../auth/passwordPolicy'
import { getStatus } from '../utils/apiError'
import type { UserRead } from '../api/types'
import {
  avatarPreCheckError,
  buildIdentityPayload,
  canSaveIdentity,
  profileDisplayName,
} from './profile'

/**
 * Self-service profile screen. Two independent sections:
 *
 *  A) Identity — edit first/last name (PATCH /auth/me). Email is shown read-only:
 *     a self-service user can never change role/disabled/email (the backend
 *     schema makes those unreachable).
 *  B) Change password — current/new/confirm with the SHARED live policy checklist
 *     (auth/passwordPolicy), posting current + new to /auth/change-password.
 *
 * Above the identity form the avatar (photo, or initials when there is none)
 * comes with the upload/remove photo buttons.
 */
export function ProfilePage() {
  const { token, user } = useAuth()

  if (!token) return <Navigate to="/login" replace />
  if (!user) {
    return (
      <Layout>
        <p className="info-msg">Loading the profile…</p>
      </Layout>
    )
  }

  return <ProfileView user={user} />
}

/** Inner view — only rendered once `user` is known, so fields seed cleanly. */
function ProfileView({ user }: { user: UserRead }) {
  const toast = useToast()
  const qc = useQueryClient()

  // ── Avatar ──────────────────────────────────────────────────────────────────
  // Fetched once under ['avatar-me'] and shared with the UserMenu; gated on the
  // cached has_avatar so we don't request a non-existent photo.
  const { url: avatarUrl } = useAvatarImage(user.has_avatar)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const uploadAvatarM = useMutation<UserRead, unknown, File>({
    mutationFn: (file) => api.uploadMyAvatar(file),
    onSuccess: (updated) => {
      setUser(updated)
      qc.invalidateQueries({ queryKey: ['avatar-me'] })
      toast.success('Profile photo updated')
    },
    onError: (err) => toast.error(err),
  })

  const deleteAvatarM = useMutation<UserRead, unknown, void>({
    mutationFn: () => api.deleteMyAvatar(),
    onSuccess: (updated) => {
      setUser(updated)
      qc.invalidateQueries({ queryKey: ['avatar-me'] })
      toast.success('Profile photo removed')
    },
    onError: (err) => toast.error(err),
  })

  function handleAvatarPick(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    e.target.value = '' // allow re-picking the same file later
    if (!file) return
    const preErr = avatarPreCheckError(file, IMAGE_ACCEPT, IMAGE_MAX_BYTES)
    if (preErr) { toast.error(preErr); return }
    uploadAvatarM.mutate(file)
  }

  const avatarBusy = uploadAvatarM.isPending || deleteAvatarM.isPending

  // ── Section A: identity ─────────────────────────────────────────────────────
  const [firstName, setFirstName] = useState(user.first_name ?? '')
  const [lastName, setLastName] = useState(user.last_name ?? '')

  // Re-seed if the cached profile changes underneath us (e.g. another tab/refresh).
  useEffect(() => {
    setFirstName(user.first_name ?? '')
    setLastName(user.last_name ?? '')
  }, [user.first_name, user.last_name])

  const identityM = useMutation<UserRead, unknown, void>({
    mutationFn: () => api.updateProfile(buildIdentityPayload(firstName, lastName)),
    onSuccess: (updated) => {
      setUser(updated)
      toast.success('Profile updated')
    },
    onError: (err) => toast.error(err),
  })

  const canSave = canSaveIdentity(user, firstName, lastName, identityM.isPending)

  function handleIdentitySave(e: FormEvent) {
    e.preventDefault()
    if (!canSave) return
    identityM.mutate()
  }

  // ── Section B: self-service password change ─────────────────────────────────
  const [currentPw, setCurrentPw] = useState('')
  const [newPw, setNewPw] = useState('')
  const [confirmPw, setConfirmPw] = useState('')
  const [currentPwError, setCurrentPwError] = useState('')

  const checks = passwordChecks(newPw, confirmPw)

  const passwordM = useMutation<UserRead, unknown, void>({
    mutationFn: () => api.changePassword(newPw, currentPw),
    onSuccess: () => {
      // Self-service keeps must_change_password=false; the returned profile is
      // unchanged in practice, so we do NOT call setUser here.
      setCurrentPw('')
      setNewPw('')
      setConfirmPw('')
      setCurrentPwError('')
      toast.success('Password updated')
    },
    onError: (err) => {
      // 400 = wrong/missing current password → inline under "Current password".
      if (getStatus(err) === 400) {
        setCurrentPwError('The current password is not correct.')
      } else {
        toast.error(err)
      }
    },
  })

  const canChangePw =
    allChecksPass(newPw, confirmPw) && currentPw !== '' && !passwordM.isPending

  function handlePasswordChange(e: FormEvent) {
    e.preventDefault()
    if (!canChangePw) return
    setCurrentPwError('')
    passwordM.mutate()
  }

  const displayName = profileDisplayName(user)

  return (
    <Layout>
      <h1 className="mb-16">My profile</h1>

      {/* ── Identity ───────────────────────────────────────────────────────── */}
      <div className="card mb-16" style={{ maxWidth: 520 }}>
        <div className="row row-16 mb-16">
          <Avatar name={displayName} email={user.email} photoUrl={avatarUrl ?? null} size={64} />
          <div className="row row-8">
            <input
              ref={fileInputRef}
              type="file"
              accept={IMAGE_ACCEPT}
              style={{ display: 'none' }}
              onChange={handleAvatarPick}
            />
            <button type="button" className="btn btn-secondary" disabled={avatarBusy}
                    onClick={() => fileInputRef.current?.click()}>
              {uploadAvatarM.isPending ? 'Uploading…' : 'Upload photo'}
            </button>
            {user.has_avatar && (
              <button type="button" className="btn btn-secondary" disabled={avatarBusy}
                      onClick={() => {
                        if (window.confirm('Remove the profile photo?')) {
                          deleteAvatarM.mutate()
                        }
                      }}>
                {deleteAvatarM.isPending ? 'Removing…' : 'Remove'}
              </button>
            )}
          </div>
        </div>

        <form onSubmit={handleIdentitySave} className="stack stack-12">
          <div className="form-group">
            <label htmlFor="p-first">First name</label>
            <input id="p-first" required value={firstName}
                   autoComplete="given-name"
                   onChange={e => setFirstName(e.target.value)} />
          </div>
          <div className="form-group">
            <label htmlFor="p-last">Last name</label>
            <input id="p-last" required value={lastName}
                   autoComplete="family-name"
                   onChange={e => setLastName(e.target.value)} />
          </div>
          <div className="form-group">
            <label htmlFor="p-email">Email</label>
            <input id="p-email" type="email" value={user.email} disabled readOnly />
            <p className="info-msg">Used to log in; it cannot be changed.</p>
          </div>
          <div className="row row-8">
            <button type="submit" className="btn btn-primary" disabled={!canSave}>
              {identityM.isPending ? 'Saving…' : 'Save'}
            </button>
          </div>
        </form>
      </div>

      {/* ── Change password (self-service) ─────────────────────────────────── */}
      <div className="card" style={{ maxWidth: 520 }}>
        <h2 className="mb-16">Change password</h2>
        <form onSubmit={handlePasswordChange} className="stack stack-12">
          <div className="form-group">
            <label htmlFor="p-current">Current password</label>
            <input id="p-current" type="password" autoComplete="current-password"
                   required value={currentPw}
                   onChange={e => { setCurrentPw(e.target.value); setCurrentPwError('') }} />
            {currentPwError && <p className="error-msg">{currentPwError}</p>}
          </div>
          <div className="form-group">
            <label htmlFor="p-new">New password</label>
            <input id="p-new" type="password" autoComplete="new-password"
                   required value={newPw}
                   onChange={e => setNewPw(e.target.value)} />
          </div>
          <div className="form-group">
            <label htmlFor="p-confirm">Confirm new password</label>
            <input id="p-confirm" type="password" autoComplete="new-password"
                   required value={confirmPw}
                   onChange={e => setConfirmPw(e.target.value)} />
          </div>
          <ul
            className="password-checklist"
            style={{ listStyle: 'none', padding: 0, margin: 0, fontSize: '0.85rem' }}
          >
            {checks.map(c => (
              <li key={c.id} style={{ color: c.ok ? 'var(--success)' : 'var(--text-muted)' }}>
                <span aria-hidden="true">{c.ok ? '✓' : '○'}</span> {c.label}
              </li>
            ))}
          </ul>
          <div className="row row-8">
            <button type="submit" className="btn btn-primary" disabled={!canChangePw}>
              {passwordM.isPending ? 'Updating…' : 'Update password'}
            </button>
          </div>
        </form>
      </div>
    </Layout>
  )
}
