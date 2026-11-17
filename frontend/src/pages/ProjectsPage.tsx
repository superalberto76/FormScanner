// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Layout } from '../components/Layout'
import { StatusBadge } from '../components/StatusBadge'
import { ProjectForm } from '../components/ProjectForm'
import { MemberAvatar } from '../components/MemberAvatar'
import { ProjectMembersModal } from '../components/ProjectMembersModal'
import { memberDisplayName } from '../components/memberDisplay'
import { useToast } from '../components/ToastProvider'
import { useAuth } from '../auth/authStore'
import { normalizeApiError } from '../utils/apiError'
import { nd, type FormProjectCreate, type FormProjectListItem } from '../api/domain'

function fmtDate(value: string | null): string {
  return value ? value.slice(0, 10) : '—'
}

/**
 * Overlapping avatar stack for a project row: up to 3 members
 * (owners first, from `members_preview`) + a "+N" chip for the rest. Rendered
 * from the list payload — NO extra fetch per row. Clicking opens the members
 * modal; the click is stopped from bubbling to the row's navigation.
 */
function AvatarStack({ project, onOpen }: { project: FormProjectListItem; onOpen: () => void }) {
  const shown = project.members_preview
  const extra = project.member_count - shown.length
  return (
    <span
      className="avatar-stack"
      role="button"
      tabIndex={0}
      aria-label="Show the project members"
      onClick={(e) => { e.stopPropagation(); onOpen() }}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); onOpen() }
      }}
    >
      {shown.map(m => (
        <MemberAvatar
          key={m.user_id}
          userId={m.user_id}
          name={memberDisplayName(m.first_name, m.last_name)}
          hasAvatar={m.has_avatar}
          size={28}
        />
      ))}
      {extra > 0 && <span className="avatar-stack-more">+{extra}</span>}
    </span>
  )
}

export function ProjectsPage() {
  const navigate = useNavigate()
  const toast = useToast()
  const qc = useQueryClient()
  const { user } = useAuth()
  const isAdmin = user?.role === 'ADMIN'
  const [showForm, setShowForm] = useState(false)
  const [membersFor, setMembersFor] = useState<FormProjectListItem | null>(null)

  const projectsQ = useQuery({
    queryKey: ['projects'],
    queryFn: nd.listProjects,
  })

  const createM = useMutation({
    mutationFn: (data: FormProjectCreate) => nd.createProject(data),
    onSuccess: () => {
      toast.success('Project created')
      setShowForm(false)
      void qc.invalidateQueries({ queryKey: ['projects'] })
    },
    onError: (err) => toast.error(err),
  })

  return (
    <Layout>
      <div className="row mb-16" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
        <h1>Projects</h1>
        {!showForm && (
          <button className="btn btn-primary" onClick={() => setShowForm(true)}>+ New project</button>
        )}
      </div>

      {showForm && (
        <div className="card mb-16">
          <h2 className="mb-16">New project</h2>
          <ProjectForm
            submitting={createM.isPending}
            onSubmit={createM.mutate}
            onCancel={() => setShowForm(false)}
          />
        </div>
      )}

      {projectsQ.isLoading && <p className="info-msg">Loading projects…</p>}
      {projectsQ.isError && <p className="error-msg">{normalizeApiError(projectsQ.error)}</p>}

      {projectsQ.data && projectsQ.data.length === 0 && (
        <div className="card"><p className="info-msg">No projects yet. Create your first one.</p></div>
      )}

      {projectsQ.data && projectsQ.data.length > 0 && (
        <div className="card" style={{ padding: 0, overflowX: 'auto' }}>
          <table className="nd-table">
            <thead>
              <tr>
                <th>Name</th><th>Type</th><th>Event date</th><th>Variants</th><th>Status</th>
                <th style={{ textAlign: 'right' }}>Members</th>
              </tr>
            </thead>
            <tbody>
              {projectsQ.data.map(p => (
                <tr key={p.id} className="nd-row" onClick={() => navigate(`/projects/${p.id}`)}>
                  <td><strong>{p.name}</strong></td>
                  <td>{p.type}</td>
                  <td>{fmtDate(p.event_date)}</td>
                  <td>{p.variant_count}</td>
                  <td><StatusBadge status={p.status} /></td>
                  <td style={{ textAlign: 'right' }}>
                    <AvatarStack project={p} onOpen={() => setMembersFor(p)} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {membersFor && (
        <ProjectMembersModal
          projectId={membersFor.id}
          projectName={membersFor.name}
          // Owners manage their projects; admins manage ANY project (the BE
          // already bypasses the owner check for admins) — e.g. to reassign
          // ownership after the sole owner leaves.
          canManage={membersFor.is_owner || isAdmin}
          onClose={() => setMembersFor(null)}
        />
      )}
    </Layout>
  )
}
