// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState, type FormEvent } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useToast } from './ToastProvider'
import { useConfirm } from './ConfirmProvider'
import { StatusBadge } from './StatusBadge'
import { normalizeApiError } from '../utils/apiError'
import { formatScore } from '../utils/scoreFormat'
import { downloadVariantPdf, nd, type ExportMode, type FormVariantDetail } from '../api/domain'

interface Props {
  variant: FormVariantDetail
}

/** Header: identity + lifecycle (rename, publish, clone) + PDF export. */
export function VariantToolbar({ variant }: Props) {
  const toast = useToast()
  const confirm = useConfirm()
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [exporting, setExporting] = useState<ExportMode | null>(null)
  const [editing, setEditing] = useState(false)

  // Maximum score of the variant: the server's value only, so
  // authoring and the submission score share one formula. Refreshed by the
  // invalidation of ['variant-max-score'] after every save that can change it;
  // hidden while loading or on error.
  const maxScoreQ = useQuery({
    queryKey: ['variant-max-score', variant.id],
    queryFn: () => nd.getVariantMaxScore(variant.id),
  })

  // Combined project×variant gating. The variant must be DRAFT to mutate
  // at all; the parent project's lifecycle narrows which actions remain allowed.
  const isDraft = variant.status === 'DRAFT'
  const projectStatus = variant.form_project_status
  const projectEditable = projectStatus === 'DRAFT'
  const canEditMeta = isDraft && projectEditable                               // rename/description
  const canPublish  = isDraft && (projectStatus === 'DRAFT' || projectStatus === 'ACTIVE')
  const canClone    = projectEditable                                          // clone = create variant → DRAFT-only
  const canDelete   = isDraft && projectStatus !== 'ARCHIVED'                  // housekeeping, not in ARCHIVED

  const publishM = useMutation({
    mutationFn: () => nd.publishVariant(variant.id),
    onSuccess: () => {
      toast.success('Variant published')
      void qc.invalidateQueries({ queryKey: ['variant', variant.id] })
    },
    onError: (err) => toast.error(err),  // publish gate failures surface the backend reason
  })

  const cloneM = useMutation({
    mutationFn: () => nd.cloneVariant(variant.id),
    onSuccess: (created) => {
      toast.success(`Cloned as v${created.version_number}`)
      void qc.invalidateQueries({ queryKey: ['variants', variant.form_project_id] })
      // Switch to the new DRAFT; the workspace re-queries pages/objects for it.
      navigate(`/variants/${created.id}`)
    },
    onError: (err) => toast.error(err),
  })

  const deleteM = useMutation({
    mutationFn: () => nd.deleteVariant(variant.id),
    onSuccess: () => {
      toast.success('Variant deleted')
      void qc.invalidateQueries({ queryKey: ['variants', variant.form_project_id] })
      void qc.invalidateQueries({ queryKey: ['variant', variant.id] })
      // Leave the workspace: the variant we were editing no longer exists.
      navigate(`/projects/${variant.form_project_id}`)
    },
    onError: (err) => toast.error(err),  // 409 (non-DRAFT) surfaces the backend reason
  })

  async function exportPdf(mode: ExportMode) {
    setExporting(mode)
    try {
      await downloadVariantPdf(variant.id, mode)
    } catch (err) {
      toast.error(err)
    } finally {
      setExporting(null)
    }
  }

  return (
    <div className="workspace-toolbar">
      <div className="stack stack-4" style={{ marginRight: 'auto' }}>
        <div className="row row-8" style={{ alignItems: 'center' }}>
          <strong>{variant.name}</strong>
          <StatusBadge status={variant.status} />
          {isDraft && (
            <button className="btn btn-secondary btn-sm"
                    disabled={!canEditMeta}
                    onClick={() => setEditing(true)}
                    title={canEditMeta
                      ? 'Rename / edit description'
                      : `Project is ${projectStatus}; content is editable only while DRAFT.`}>
              Edit
            </button>
          )}
        </div>
        <span className="nd-muted" style={{ fontSize: '0.8rem' }}>
          <code>{variant.root_key}</code> · v{variant.version_number}
          {variant.description ? ` · ${variant.description}` : ''}
          {maxScoreQ.data && ` · Max score: ${formatScore(maxScoreQ.data.max_score)}`}
        </span>
      </div>

      <button className="btn btn-primary btn-sm"
              disabled={!canPublish || publishM.isPending}
              title={!isDraft
                ? 'Already published/archived'
                : !canPublish
                  ? `Project is ${projectStatus}; publish is allowed only in DRAFT or ACTIVE.`
                  : 'Publish this variant'}
              onClick={async () => {
                const ok = await confirm({
                  title: 'Publish variant',
                  message: `Publish "${variant.name}"? Published variants become read-only; clone to keep editing.`,
                  confirmLabel: 'Publish',
                })
                if (ok) publishM.mutate()
              }}>
        {publishM.isPending ? 'Publishing…' : 'Publish'}
      </button>
      <button className="btn btn-secondary btn-sm"
              disabled={!canClone || cloneM.isPending}
              title={canClone
                ? undefined
                : `Project is ${projectStatus}; cloning creates a new variant and is allowed only while DRAFT.`}
              onClick={async () => {
                const ok = await confirm({
                  title: 'Clone variant',
                  message: `Create a new DRAFT copy of "${variant.name}"? You will switch to the clone.`,
                  confirmLabel: 'Clone',
                })
                if (ok) cloneM.mutate()
              }}>
        {cloneM.isPending ? 'Cloning…' : 'Clone'}
      </button>
      <button className="btn btn-secondary btn-sm" disabled={exporting !== null} onClick={() => exportPdf('STUDENT')}>
        {exporting === 'STUDENT' ? 'Preparing…' : 'Download Student PDF'}
      </button>
      <button className="btn btn-secondary btn-sm" disabled={exporting !== null} onClick={() => exportPdf('SOLUTION')}>
        {exporting === 'SOLUTION' ? 'Preparing…' : 'Download Solution PDF'}
      </button>
      {isDraft && (
        <button className="btn btn-secondary btn-sm"
                style={{ color: 'var(--danger)' }}
                disabled={!canDelete || deleteM.isPending}
                title={canDelete
                  ? 'Delete this DRAFT variant and all its objects'
                  : 'Project is ARCHIVED; nothing can be deleted.'}
                onClick={async () => {
                  const ok = await confirm({
                    title: 'Delete variant',
                    message: `This will permanently delete the variant "${variant.name}" and all its objects. This cannot be undone.`,
                    confirmLabel: 'Delete',
                    danger: true,
                  })
                  if (ok) deleteM.mutate()
                }}>
          {deleteM.isPending ? 'Deleting…' : 'Delete'}
        </button>
      )}

      {editing && <VariantEditModal variant={variant} onClose={() => setEditing(false)} />}
    </div>
  )
}

function VariantEditModal({ variant, onClose }: { variant: FormVariantDetail; onClose: () => void }) {
  const toast = useToast()
  const qc = useQueryClient()
  const [name, setName] = useState(variant.name)
  const [description, setDescription] = useState(variant.description ?? '')

  const m = useMutation({
    mutationFn: () => nd.updateVariant(variant.id, { name: name.trim(), description: description.trim() || null }),
    onSuccess: () => {
      toast.success('Variant updated')
      void qc.invalidateQueries({ queryKey: ['variant', variant.id] })
      void qc.invalidateQueries({ queryKey: ['variants', variant.form_project_id] })
      onClose()
    },
    onError: (err) => toast.error(err),
  })

  function submit(e: FormEvent) { e.preventDefault(); if (name.trim()) m.mutate() }

  // Portal to <body> so it covers the canvas overlays/handles.
  return createPortal(
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-box" style={{ width: 420 }} onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h2>Edit variant</h2>
          <button className="btn btn-secondary btn-sm" onClick={onClose}>✕</button>
        </div>
        <form onSubmit={submit} className="stack stack-12">
          <div className="form-group"><label>Name</label><input required value={name} onChange={e => setName(e.target.value)} /></div>
          <div className="form-group"><label>Description</label><textarea rows={3} value={description} onChange={e => setDescription(e.target.value)} /></div>
          {m.isError && <p className="error-msg">{normalizeApiError(m.error)}</p>}
          <div className="row row-8">
            <button type="submit" className="btn btn-primary" disabled={m.isPending || !name.trim()}>
              {m.isPending ? 'Saving…' : 'Save'}
            </button>
            <button type="button" className="btn btn-secondary" onClick={onClose} disabled={m.isPending}>Cancel</button>
          </div>
        </form>
      </div>
    </div>,
    document.body,
  )
}
