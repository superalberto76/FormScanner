// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useState } from 'react'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Layout } from '../components/Layout'
import { ResetPasswordModal } from '../components/ResetPasswordModal'
import { useConfirm } from '../components/ConfirmProvider'
import { useToast } from '../components/ToastProvider'
import { api } from '../api/endpoints'
import { normalizeApiError } from '../utils/apiError'
import type { UserListItem } from '../api/types'
import {
  canActOnUser,
  hasNext,
  hasPrev,
  nameOrDash,
  pageSummary,
  roleBadgeClass,
  statusBadgeClass,
  statusLabel,
  userDisplayName,
} from './adminUsers'

const PAGE_SIZE = 20
const SEARCH_DEBOUNCE_MS = 300

/**
 * Admin-only "Manage users" screen. Lists users with name / email
 * / role / status; searchable by first name, last name or email and paginated.
 * The "Actions" column carries the row actions (disable/enable + password
 * reset), shown for USER rows only — ADMIN rows get a muted "—" (the BE rejects
 * ADMIN targets with 409; the FE just hides the controls).
 *
 * Admin gating: the BE `require_admin` is authoritative; this page is only
 * reachable behind `AdminRoute` and from an admin-only menu entry.
 */
export function AdminUsersPage() {
  const qc = useQueryClient()
  const confirm = useConfirm()
  const toast = useToast()
  // The user whose password-reset modal is open (null = closed).
  const [resetTarget, setResetTarget] = useState<UserListItem | null>(null)

  // `q` is the live input; `debouncedQ` is what we actually query with (~300ms
  // after the last keystroke) so typing doesn't fire a request per character.
  const [q, setQ] = useState('')
  const [debouncedQ, setDebouncedQ] = useState('')
  const [offset, setOffset] = useState(0)

  useEffect(() => {
    const id = setTimeout(() => setDebouncedQ(q), SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(id)
  }, [q])

  // A new search must start from the first page, or the user could be stranded
  // on an out-of-range page (e.g. page 3 of a result set that now has one page).
  useEffect(() => {
    setOffset(0)
  }, [debouncedQ])

  const usersQ = useQuery({
    queryKey: ['admin-users', { q: debouncedQ, limit: PAGE_SIZE, offset }],
    queryFn: () => api.listUsers({ q: debouncedQ, limit: PAGE_SIZE, offset }),
    // Keep the previous page on screen while the next one loads, so paging and
    // typing never flash an empty table.
    placeholderData: keepPreviousData,
  })

  const items = usersQ.data?.items ?? []
  const total = usersQ.data?.total ?? 0

  // Enable/disable a USER. Invalidating the whole 'admin-users' key refreshes the
  // current page/search (and any other cached page) after the change.
  const disabledM = useMutation({
    mutationFn: (v: { userId: string; disabled: boolean }) =>
      api.setUserDisabled(v.userId, v.disabled),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ['admin-users'] }) },
    onError: (err) => toast.error(err),
  })

  async function onToggleDisabled(u: UserListItem) {
    // Enable is immediate; disable asks for confirmation first (danger).
    if (u.disabled) {
      disabledM.mutate({ userId: u.id, disabled: false })
      return
    }
    const ok = await confirm({
      title: 'Disable user',
      message: `Disable ${userDisplayName(u)}? They will no longer be able to log in.`,
      confirmLabel: 'Disable',
      danger: true,
    })
    if (ok) disabledM.mutate({ userId: u.id, disabled: true })
  }

  return (
    <Layout>
      <h1 className="mb-16">Manage users</h1>

      <div className="card mb-16">
        <div className="form-group" style={{ margin: 0 }}>
          <label htmlFor="user-search">Search users</label>
          <input
            id="user-search"
            type="search"
            value={q}
            onChange={e => setQ(e.target.value)}
            placeholder="Search by first name, last name or email"
          />
        </div>
      </div>

      {usersQ.isError && (
        <p className="error-msg">{normalizeApiError(usersQ.error)}</p>
      )}

      <div className="card" style={{ padding: 0, overflowX: 'auto' }}>
        <table className="nd-table">
          <thead>
            <tr>
              <th>First name</th>
              <th>Last name</th>
              <th>Email</th>
              <th>Role</th>
              <th>Status</th>
              {/* Disable toggle + password reset live in this column. */}
              <th style={{ textAlign: 'right' }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {items.map(u => (
              <tr key={u.id}>
                <td>{nameOrDash(u.first_name)}</td>
                <td>{nameOrDash(u.last_name)}</td>
                <td>{u.email}</td>
                <td><span className={`badge ${roleBadgeClass(u.role)}`}>{u.role}</span></td>
                <td>
                  <span className={`badge ${statusBadgeClass(u.disabled)}`}>
                    {statusLabel(u.disabled)}
                  </span>
                </td>
                <td style={{ textAlign: 'right' }}>
                  {canActOnUser(u.role) ? (
                    <span className="row row-8" style={{ justifyContent: 'flex-end' }}>
                      <button
                        type="button"
                        className="btn btn-secondary btn-sm"
                        disabled={disabledM.isPending}
                        onClick={() => onToggleDisabled(u)}
                      >
                        {u.disabled ? 'Enable' : 'Disable'}
                      </button>
                      <button
                        type="button"
                        className="btn btn-secondary btn-sm"
                        onClick={() => setResetTarget(u)}
                      >
                        Reset password
                      </button>
                    </span>
                  ) : (
                    // ADMIN rows have no actions — mirrors the BE 409 guard.
                    <span className="nd-muted" aria-label="No actions">—</span>
                  )}
                </td>
              </tr>
            ))}
            {!usersQ.isLoading && items.length === 0 && (
              <tr>
                <td colSpan={6} className="info-msg" style={{ textAlign: 'center' }}>
                  No users found.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div
        className="row mt-16"
        style={{ justifyContent: 'space-between', alignItems: 'center' }}
      >
        <span className="info-msg">{pageSummary(offset, items.length, total)}</span>
        <div className="row row-8">
          <button
            type="button"
            className="btn"
            disabled={!hasPrev(offset)}
            onClick={() => setOffset(o => Math.max(0, o - PAGE_SIZE))}
          >
            ← Previous
          </button>
          <button
            type="button"
            className="btn"
            disabled={!hasNext(offset, PAGE_SIZE, total)}
            onClick={() => setOffset(o => o + PAGE_SIZE)}
          >
            Next →
          </button>
        </div>
      </div>

      {usersQ.isLoading && <p className="info-msg mt-16">Loading…</p>}

      {resetTarget && (
        <ResetPasswordModal user={resetTarget} onClose={() => setResetTarget(null)} />
      )}
    </Layout>
  )
}
