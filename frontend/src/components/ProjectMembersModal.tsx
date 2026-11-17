// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { nd, type FormProjectMember, type ProjectRole, type UserSearchItem } from '../api/domain'
import { useAuth } from '../auth/authStore'
import { useConfirm } from './ConfirmProvider'
import { useToast } from './ToastProvider'
import { MemberAvatar } from './MemberAvatar'
import { memberDisplayName, roleLabel } from './memberDisplay'

interface Props {
  projectId: string
  /** Project name for the header (optional — avoids an extra fetch). */
  projectName?: string
  /** The row's `is_owner`: gates the management actions. */
  canManage: boolean
  onClose: () => void
}

/**
 * Project members modal. Read-only roster for any member;
 * owners (and admins, server-side) also get add/remove/promote/demote. Rendered
 * in a portal above the list, like the other modals. Avatars show the user's
 * photo when `has_avatar` is set, otherwise coloured initials (MemberAvatar).
 */
export function ProjectMembersModal({ projectId, projectName, canManage, onClose }: Props) {
  const { user } = useAuth()
  const qc = useQueryClient()
  const confirm = useConfirm()
  const toast = useToast()
  const [showAdd, setShowAdd] = useState(false)

  const membersQ = useQuery({
    queryKey: ['project-members', projectId],
    queryFn: () => nd.listProjectMembers(projectId),
  })

  // Every mutation can change the stack avatars + is_owner on the list row.
  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['project-members', projectId] })
    void qc.invalidateQueries({ queryKey: ['projects'] })
  }

  const addM = useMutation({
    mutationFn: (userId: string) => nd.addProjectMember(projectId, userId),
    onSuccess: () => { toast.success('Collaborator added'); invalidate() },
    onError: (err) => toast.error(err),
  })
  const roleM = useMutation({
    mutationFn: (v: { userId: string; role: ProjectRole }) =>
      nd.setProjectMemberRole(projectId, v.userId, v.role),
    onSuccess: () => invalidate(),
    onError: (err) => toast.error(err),
  })
  const removeM = useMutation({
    mutationFn: (userId: string) => nd.removeProjectMember(projectId, userId),
    onSuccess: () => { toast.success('Member removed'); invalidate() },
    onError: (err) => toast.error(err),
  })

  const members = membersQ.data ?? []
  const owners = members.filter(m => m.role === 'OWNER')
  const collaborators = members.filter(m => m.role === 'COLLABORATOR')
  const ownerCount = owners.length
  const busy = addM.isPending || roleM.isPending || removeM.isPending

  function labelFor(m: FormProjectMember): string {
    const name = memberDisplayName(m.first_name, m.last_name) || 'User'
    return m.user_id === user?.id ? `${name} (you)` : name
  }

  async function onDemote(m: FormProjectMember) {
    const ok = await confirm({
      title: 'Remove owner role',
      message: `Demote ${memberDisplayName(m.first_name, m.last_name) || 'this user'} to collaborator?`,
      confirmLabel: 'Demote',
      danger: true,
    })
    if (ok) roleM.mutate({ userId: m.user_id, role: 'COLLABORATOR' })
  }

  async function onRemove(m: FormProjectMember) {
    const ok = await confirm({
      title: 'Remove collaborator',
      message: `Remove ${memberDisplayName(m.first_name, m.last_name) || 'this user'} from the project? They will lose access.`,
      confirmLabel: 'Remove',
      danger: true,
    })
    if (ok) removeM.mutate(m.user_id)
  }

  function renderRow(m: FormProjectMember) {
    const isSelf = m.user_id === user?.id
    const isOwner = m.role === 'OWNER'
    return (
      <div key={m.user_id} className="nd-member-row">
        <MemberAvatar
          userId={m.user_id}
          name={memberDisplayName(m.first_name, m.last_name)}
          hasAvatar={m.has_avatar}
          size={32}
        />
        <span className="nd-member-name">{labelFor(m)}</span>
        <span className={`badge ${isOwner ? 'badge-role-owner' : 'badge-role-collaborator'}`}>
          {roleLabel(m.role)}
        </span>
        {canManage && (
          <span className="nd-member-actions">
            {isOwner ? (
              // Demote (→COLLABORATOR). Hidden on your own row and when this is
              // the last owner (the BE enforces both regardless).
              !isSelf && ownerCount > 1 && (
                <button type="button" className="btn btn-secondary btn-sm"
                        disabled={busy} onClick={() => onDemote(m)}>
                  Remove owner
                </button>
              )
            ) : (
              <>
                <button type="button" className="btn btn-secondary btn-sm"
                        disabled={busy}
                        onClick={() => roleM.mutate({ userId: m.user_id, role: 'OWNER' })}>
                  Make owner
                </button>
                <button type="button" className="btn btn-secondary btn-sm"
                        disabled={busy} onClick={() => onRemove(m)} title="Remove from the project">
                  🗑
                </button>
              </>
            )}
          </span>
        )}
      </div>
    )
  }

  return createPortal(
    <div className="modal-backdrop" onClick={busy ? undefined : onClose}>
      <div className="modal-box" style={{ width: 520, maxWidth: '92vw' }} onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h2>Project members{projectName ? ` — ${projectName}` : ''}</h2>
          <button className="btn btn-secondary btn-sm" onClick={onClose} disabled={busy}>✕</button>
        </div>

        {membersQ.isLoading && <p className="info-msg">Loading…</p>}
        {membersQ.isError && <p className="error-msg">Could not load the members.</p>}

        {membersQ.data && (
          <div className="stack stack-16">
            <section>
              <h3 className="nd-member-section-title">Owner</h3>
              <div className="stack stack-4">{owners.map(renderRow)}</div>
            </section>

            <section>
              <h3 className="nd-member-section-title">Collaborators</h3>
              {collaborators.length === 0
                ? <p className="nd-muted">No collaborators.</p>
                : <div className="stack stack-4">{collaborators.map(renderRow)}</div>}
            </section>

            {canManage && (
              <>
                {showAdd
                  ? <AddCollaboratorPanel
                      projectId={projectId}
                      currentMemberIds={members.map(m => m.user_id)}
                      busy={addM.isPending}
                      onAdd={(userId) => addM.mutate(userId)}
                      onClose={() => setShowAdd(false)}
                    />
                  : <button type="button" className="btn btn-primary btn-sm"
                            onClick={() => setShowAdd(true)}>
                      Add collaborators
                    </button>}
                <p className="nd-muted nd-edit-hint">
                  There must always be at least one owner; you cannot remove or demote yourself.
                </p>
              </>
            )}
          </div>
        )}
      </div>
    </div>,
    document.body,
  )
}

interface AddPanelProps {
  projectId: string
  currentMemberIds: string[]
  busy: boolean
  onAdd: (userId: string) => void
  onClose: () => void
}

/** Debounced user-search (≥2 chars) for adding collaborators (owner-only). */
function AddCollaboratorPanel({ projectId, currentMemberIds, busy, onAdd, onClose }: AddPanelProps) {
  const [q, setQ] = useState('')
  const [debouncedQ, setDebouncedQ] = useState('')

  useEffect(() => {
    const id = setTimeout(() => setDebouncedQ(q), 300)
    return () => clearTimeout(id)
  }, [q])

  const searchQ = useQuery({
    queryKey: ['user-search', projectId, debouncedQ],
    queryFn: () => nd.searchUsers(debouncedQ, projectId),
    enabled: debouncedQ.trim().length >= 2,
  })

  const memberIds = useMemo(() => new Set(currentMemberIds), [currentMemberIds])
  // BE already excludes current members; also hide them client-side defensively.
  const results: UserSearchItem[] = (searchQ.data ?? []).filter(u => !memberIds.has(u.id))

  return (
    <div className="nd-panel-section">
      <div className="row mb-8" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
        <strong>Add collaborators</strong>
        <button type="button" className="btn btn-secondary btn-sm" onClick={onClose}>Close</button>
      </div>
      <input
        type="text"
        value={q}
        placeholder="Search by name or email (min 2 characters)…"
        onChange={e => setQ(e.target.value)}
        autoFocus
      />
      {debouncedQ.trim().length >= 2 && (
        <div className="stack stack-4 mt-8">
          {searchQ.isLoading && <p className="info-msg">Searching…</p>}
          {searchQ.isError && <p className="error-msg">Search failed.</p>}
          {searchQ.data && results.length === 0 && (
            <p className="nd-muted">No users found</p>
          )}
          {results.map(u => {
            const name = memberDisplayName(u.first_name, u.last_name) || u.email
            return (
              <div key={u.id} className="nd-member-row">
                <MemberAvatar userId={u.id} name={name} hasAvatar={u.has_avatar} email={u.email} size={32} />
                <span className="nd-member-name">
                  {name}
                  <span className="nd-muted" style={{ marginLeft: 6 }}>{u.email}</span>
                </span>
                <span className="nd-member-actions">
                  <button type="button" className="btn btn-primary btn-sm"
                          disabled={busy} onClick={() => onAdd(u.id)}>
                    Add
                  </button>
                </span>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
