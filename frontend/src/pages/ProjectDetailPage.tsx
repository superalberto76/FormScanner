// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useState } from 'react'
import { Link, Navigate, useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Layout } from '../components/Layout'
import { StatusBadge } from '../components/StatusBadge'
import { ProjectLifecycleActions } from '../components/ProjectLifecycleActions'
import {
  ProjectDetailTabs,
  SCANNING_REQUIRES_ACTIVE,
  type ProjectDetailTab,
} from '../components/ProjectDetailTabs'
import { VariantForm } from '../components/VariantForm'
import { SubmissionBatchForm } from '../components/SubmissionBatchForm'
import { ProjectBatchesList } from '../components/ProjectBatchesList'
import { useToast } from '../components/ToastProvider'
import { useConfirm } from '../components/ConfirmProvider'
import { useAuth } from '../auth/authStore'
import { getErrorCode, normalizeApiError } from '../utils/apiError'
import {
  nd,
  type FormVariantCreate,
  type FormVariantListItem,
  type SubmissionBatchCreate,
} from '../api/domain'

// Structured 409 of batch creation and uploads: the project's published
// variants are not configured for scanning.
const NOT_SCAN_READY = 'NOT_SCAN_READY'

function fmtDate(value: string | null): string {
  return value ? value.slice(0, 10) : '—'
}

export function ProjectDetailPage() {
  const { projectId = '', tab } = useParams()
  const navigate = useNavigate()
  const toast = useToast()
  const confirm = useConfirm()
  const qc = useQueryClient()
  const { user } = useAuth()
  const [showForm, setShowForm] = useState(false)
  const [showBatchForm, setShowBatchForm] = useState(false)

  const projectQ = useQuery({
    queryKey: ['project', projectId],
    queryFn: () => nd.getProject(projectId),
    enabled: !!projectId,
  })

  const variantsQ = useQuery({
    queryKey: ['variants', projectId],
    queryFn: () => nd.listVariants(projectId),
    enabled: !!projectId,
  })

  const createM = useMutation({
    mutationFn: (data: FormVariantCreate) => nd.createVariant(projectId, data),
    onSuccess: (created) => {
      toast.success('Variant created')
      setShowForm(false)
      void qc.invalidateQueries({ queryKey: ['variants', projectId] })
      navigate(`/variants/${created.id}`)
    },
    onError: (err) => toast.error(err),
  })

  const createBatchM = useMutation({
    mutationFn: (data: SubmissionBatchCreate) => nd.createSubmissionBatch(projectId, data),
    onSuccess: (created) => {
      toast.success('Batch created')
      setShowBatchForm(false)
      // The Scanning tab's batch list reads this key.
      void qc.invalidateQueries({ queryKey: ['submission-batches', projectId] })
      navigate(`/projects/${projectId}/scanning/batches/${created.id}`)
    },
    // No onError toast on purpose: a failed creation (409 — project no longer
    // ACTIVE, or not ready for scanning) is shown inline in the open panel, so
    // the typed-in values survive.
    onError: (err) => {
      // Our readiness was stale: refetch it so the banner says why.
      if (getErrorCode(err) === NOT_SCAN_READY) {
        void qc.invalidateQueries({ queryKey: ['scan-readiness', projectId] })
      }
    },
  })

  const deleteVariantM = useMutation({
    mutationFn: (variantId: string) => nd.deleteVariant(variantId),
    onSuccess: (_void, variantId) => {
      toast.success('Variant deleted')
      void qc.invalidateQueries({ queryKey: ['variants', projectId] })
      void qc.invalidateQueries({ queryKey: ['variant', variantId] })
    },
    onError: (err) => toast.error(err),  // 409 (non-DRAFT) surfaces the backend reason
  })

  const deleteProjectM = useMutation({
    mutationFn: () => nd.deleteProject(projectId),
    onSuccess: () => {
      toast.success('Project deleted')
      void qc.invalidateQueries({ queryKey: ['projects'] })
      navigate('/projects')
    },
    onError: (err) => toast.error(err),  // 409 (not all DRAFT) surfaces the backend reason
  })

  const variants = variantsQ.data ?? []
  // Mirror the BE gate for UX; the BE remains the authoritative defense.
  const allVariantsDraft = variants.every((v) => v.status === 'DRAFT')
  const canDeleteProject = variants.length === 0 || allVariantsDraft

  // Lifecycle transitions are owner-or-admin only (mirrors the BE owner gate).
  const isAdmin = user?.role === 'ADMIN'
  const canManageLifecycle = !!projectQ.data && (projectQ.data.is_owner || isAdmin)

  async function onDeleteVariant(v: FormVariantListItem) {
    const ok = await confirm({
      title: 'Delete variant',
      message: `This will permanently delete the variant "${v.name}" and all its objects. This cannot be undone.`,
      confirmLabel: 'Delete',
      danger: true,
    })
    if (ok) deleteVariantM.mutate(v.id)
  }

  async function onDeleteProject() {
    const ok = await confirm({
      title: 'Delete project',
      message: `This will permanently delete "${projectQ.data?.name ?? 'this project'}", all its variants and every object inside them. This cannot be undone.`,
      confirmLabel: 'Delete',
      danger: true,
    })
    if (ok) deleteProjectM.mutate()
  }

  // Anything other than the two known tabs (typo'd/stale URL) falls back to the
  // default tab rather than a 404 — this is an internal page.
  const activeTab: ProjectDetailTab | null =
    tab === 'variants' || tab === 'scanning' ? tab : null

  // Two gates, deliberately apart. The Scanning TAB opens for every project
  // that has left DRAFT — review and export happen after collection closes, so
  // a CLOSED/ARCHIVED project's batches must stay reachable. COLLECTING (a new
  // batch, and the readiness check behind it) stays ACTIVE-only, like the BE.
  // While the project is still loading both are false (fail toward the safer
  // state).
  const projectStatus = projectQ.data?.status
  const scanningEnabled =
    projectStatus === 'ACTIVE' || projectStatus === 'CLOSED' || projectStatus === 'ARCHIVED'
  const canCollect = projectStatus === 'ACTIVE'

  // Scan-readiness gates batch creation. Only worth fetching once the Scanning
  // tab is selected and the project can actually collect.
  const scanReadinessQ = useQuery({
    queryKey: ['scan-readiness', projectId],
    queryFn: () => nd.getScanReadiness(projectId),
    enabled: !!projectId && activeTab === 'scanning' && canCollect,
  })

  // The batch list. Readable on any project the tab is open for; invalidated
  // by batch creation above, never polled.
  const batchesQ = useQuery({
    queryKey: ['submission-batches', projectId],
    queryFn: () => nd.listSubmissionBatches(projectId),
    enabled: !!projectId && activeTab === 'scanning' && scanningEnabled,
  })

  // This page stays mounted across the Variants↔Scanning switch (only the :tab
  // param changes), so react-query would happily serve a stale readiness after
  // the user assigned the missing role on Variants. Invalidate on every entry
  // into Scanning — deliberately only that case (no polling, no focus listener).
  useEffect(() => {
    if (activeTab === 'scanning' && canCollect) {
      void qc.invalidateQueries({ queryKey: ['scan-readiness', projectId] })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, projectId, canCollect])

  // Fail closed, like `scanningEnabled` above: loading and a failed readiness
  // fetch are as blocking as an explicit ready:false. The POST's own 409
  // (NOT_SCAN_READY) stays the authoritative net for whatever slips
  // through, and refetches this query when it fires.
  const canCreateBatch = scanReadinessQ.data?.ready === true

  if (!activeTab) return <Navigate to={`/projects/${projectId}/variants`} replace />

  return (
    <Layout>
      <p className="nd-breadcrumb mb-16">
        <Link to="/projects">Projects</Link> <span className="nd-muted">/</span>{' '}
        {projectQ.data?.name ?? '…'}
      </p>

      {projectQ.isError && <p className="error-msg">{normalizeApiError(projectQ.error)}</p>}

      {projectQ.data && (
        <div className="card mb-16">
          <div className="row" style={{ justifyContent: 'space-between', alignItems: 'flex-start' }}>
            <h1 className="mb-16">{projectQ.data.name}</h1>
            <div className="row row-8" style={{ alignItems: 'center' }}>
              <StatusBadge status={projectQ.data.status} />
              <ProjectLifecycleActions project={projectQ.data} canManage={canManageLifecycle} />
              <button
                className="btn btn-secondary btn-sm"
                style={{ color: 'var(--danger)' }}
                disabled={!canDeleteProject || deleteProjectM.isPending}
                title={
                  canDeleteProject
                    ? 'Delete this project and all its variants'
                    : 'Project can be deleted only when all its variants are DRAFT'
                }
                onClick={onDeleteProject}
              >
                {deleteProjectM.isPending ? 'Deleting…' : 'Delete project'}
              </button>
            </div>
          </div>
          <dl className="detail-grid">
            <dt>Type</dt><dd>{projectQ.data.type}</dd>
            <dt>Event date</dt><dd>{fmtDate(projectQ.data.event_date)}</dd>
            <dt>Description</dt><dd>{projectQ.data.description || '—'}</dd>
          </dl>
        </div>
      )}

      <ProjectDetailTabs projectId={projectId} activeTab={activeTab} scanningEnabled={scanningEnabled} />

      {activeTab === 'variants' && (
        <>
          <div className="row mb-16" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
            <h2>Variants</h2>
            {!showForm && (
              <button className="btn btn-primary" onClick={() => setShowForm(true)}>+ New variant</button>
            )}
          </div>

          {showForm && (
            <div className="card mb-16">
              <h3 className="mb-16">New variant</h3>
              <VariantForm
                submitting={createM.isPending}
                onSubmit={createM.mutate}
                onCancel={() => setShowForm(false)}
              />
            </div>
          )}

          {variantsQ.isLoading && <p className="info-msg">Loading variants…</p>}
          {variantsQ.isError && <p className="error-msg">{normalizeApiError(variantsQ.error)}</p>}

          {variantsQ.data && variantsQ.data.length === 0 && (
            <div className="card"><p className="info-msg">No variants yet.</p></div>
          )}

          {variantsQ.data && variantsQ.data.length > 0 && (
            <div className="card" style={{ padding: 0, overflowX: 'auto' }}>
              <table className="nd-table">
                <thead>
                  <tr><th>Name</th><th>Root key</th><th>Version</th><th>Pages</th><th>Status</th><th></th></tr>
                </thead>
                <tbody>
                  {variantsQ.data.map(v => (
                    <tr key={v.id} className="nd-row" onClick={() => navigate(`/variants/${v.id}`)}>
                      <td><strong>{v.name}</strong></td>
                      <td><code>{v.root_key}</code></td>
                      <td>v{v.version_number}</td>
                      <td>{v.page_count}</td>
                      <td><StatusBadge status={v.status} /></td>
                      <td style={{ textAlign: 'right' }}>
                        <button
                          className="btn btn-secondary btn-sm"
                          style={{ color: 'var(--danger)' }}
                          disabled={v.status !== 'DRAFT' || deleteVariantM.isPending}
                          title={
                            v.status === 'DRAFT'
                              ? 'Delete this DRAFT variant and all its objects'
                              : 'Only DRAFT variants can be deleted'
                          }
                          onClick={(e) => { e.stopPropagation(); void onDeleteVariant(v) }}
                        >
                          Delete
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      {activeTab === 'scanning' && (
        projectQ.isLoading ? (
          <p className="info-msg">Loading project…</p>
        ) : scanningEnabled ? (
          <>
            {/* Explicit === false: while loading (or after a failed fetch) the
                button is already disabled, but there is nothing to warn about
                yet. Not dismissible — it goes away only when the project is
                actually fixed and the query refetches. */}
            {canCollect && scanReadinessQ.data?.ready === false && (
              <div className="readonly-banner mb-16" role="status">
                <div className="row row-8" style={{ alignItems: 'flex-start' }}>
                  <span aria-hidden="true">⚠️</span>
                  <div>
                    <p style={{ fontWeight: 600, marginBottom: 4 }}>Cannot start a new scan</p>
                    {/* Verbatim backend text — never recomposed or translated. */}
                    <p style={{ marginBottom: 10 }}>{scanReadinessQ.data.reason}</p>
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      style={{ color: 'var(--warning-text)', borderColor: 'var(--warning-border)' }}
                      onClick={() => navigate(`/projects/${projectId}/variants`)}
                    >
                      Go to Variants
                    </button>
                  </div>
                </div>
              </div>
            )}

            <div className="row mb-16" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
              <h2>Batches</h2>
              {canCollect && !showBatchForm && (
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={!canCreateBatch}
                  title={
                    canCreateBatch
                      ? undefined
                      : scanReadinessQ.data?.reason ?? 'Resolve the warning above to continue'
                  }
                  // reset() so a previous failure doesn't greet the next panel.
                  onClick={() => { createBatchM.reset(); setShowBatchForm(true) }}
                >
                  + New batch
                </button>
              )}
            </div>

            {!canCollect && (
              <p className="nd-gate-line">
                New batches can only be created while the project is active. Existing batches remain available for review and export.
              </p>
            )}

            {canCollect && showBatchForm && (
              <div className="card mb-16">
                <h3 className="mb-16">New batch</h3>
                <SubmissionBatchForm
                  submitting={createBatchM.isPending}
                  error={createBatchM.error}
                  onSubmit={createBatchM.mutate}
                  onCancel={() => setShowBatchForm(false)}
                />
              </div>
            )}

            {batchesQ.isLoading && <p className="info-msg">Loading batches…</p>}
            {batchesQ.isError && <p className="error-msg">{normalizeApiError(batchesQ.error)}</p>}

            {batchesQ.data && batchesQ.data.length === 0 && (
              <div className="card"><p className="info-msg">No batches yet.</p></div>
            )}

            {batchesQ.data && batchesQ.data.length > 0 && (
              <ProjectBatchesList
                batches={batchesQ.data}
                onOpen={id => navigate(`/projects/${projectId}/scanning/batches/${id}`)}
              />
            )}
          </>
        ) : (
          // Direct navigation to /scanning on a DRAFT project: keep the tab
          // selected, but show only the gate message (no header, no button).
          <div className="card"><p className="info-msg">{SCANNING_REQUIRES_ACTIVE}</p></div>
        )
      )}
    </Layout>
  )
}
