// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState } from 'react'
import { createPortal } from 'react-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/endpoints'
import { useToast } from './ToastProvider'
import { normalizeApiError } from '../utils/apiError'
import type { UserListItem } from '../api/types'
import {
  TEMP_PASSWORD_MIN_LENGTH,
  canSubmitReset,
  passwordsMismatch,
  tempPasswordTooShort,
} from '../pages/adminUsers'

interface Props {
  user: UserListItem
  onClose: () => void
}

/**
 * Admin "reset temporary password" modal. Sets a provisional
 * password and forces a change at next login. Rendered in a portal above the
 * list, like the other modals.
 *
 * Validation mirrors ONLY the backend min-length rule (≥ TEMP_PASSWORD_MIN_LENGTH)
 * plus a confirm match — deliberately NOT the rich password policy/checklist, which
 * the user must satisfy when they change this temp password at next login.
 */
export function ResetPasswordModal({ user, onClose }: Props) {
  const qc = useQueryClient()
  const toast = useToast()
  const [newPw, setNewPw] = useState('')
  const [confirmPw, setConfirmPw] = useState('')

  const resetM = useMutation({
    mutationFn: () => api.resetUserPassword(user.id, newPw),
    onSuccess: () => {
      toast.success(`Password reset for ${user.email}`)
      // Refresh the list.
      void qc.invalidateQueries({ queryKey: ['admin-users'] })
      onClose()
    },
    onError: (err) => toast.error(normalizeApiError(err)),
  })

  const canSubmit = canSubmitReset(newPw, confirmPw) && !resetM.isPending

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!canSubmit) return
    resetM.mutate()
  }

  return createPortal(
    <div className="modal-backdrop" onClick={resetM.isPending ? undefined : onClose}>
      <div className="modal-box" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h2>Reset password — {user.email}</h2>
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={onClose}
            disabled={resetM.isPending}
          >
            ✕
          </button>
        </div>

        <form onSubmit={handleSubmit} className="stack stack-12">
          <p className="info-msg">
            The user will have to change it at the next login.
          </p>

          <div className="form-group">
            <label htmlFor="reset-pw">New temporary password</label>
            <input
              id="reset-pw"
              type="password"
              autoComplete="new-password"
              value={newPw}
              onChange={e => setNewPw(e.target.value)}
              autoFocus
            />
            {tempPasswordTooShort(newPw) && (
              <p className="error-msg">At least {TEMP_PASSWORD_MIN_LENGTH} characters</p>
            )}
          </div>

          <div className="form-group">
            <label htmlFor="reset-pw-confirm">Confirm password</label>
            <input
              id="reset-pw-confirm"
              type="password"
              autoComplete="new-password"
              value={confirmPw}
              onChange={e => setConfirmPw(e.target.value)}
            />
            {passwordsMismatch(newPw, confirmPw) && (
              <p className="error-msg">The passwords do not match</p>
            )}
          </div>

          <div className="row row-8" style={{ justifyContent: 'flex-end' }}>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={onClose}
              disabled={resetM.isPending}
            >
              Cancel
            </button>
            <button type="submit" className="btn btn-primary" disabled={!canSubmit}>
              {resetM.isPending ? 'Resetting…' : 'Reset password'}
            </button>
          </div>
        </form>
      </div>
    </div>,
    document.body,
  )
}
