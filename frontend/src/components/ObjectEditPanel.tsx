// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { normalizeApiError } from '../utils/apiError'
import { OBJECT_TYPE_STYLE } from '../utils/objectDisplay'
import { holdsVariantIdentifier } from '../utils/pageObjectRole'
import { nd, type PageObjectDetail, type RelinkMode } from '../api/domain'
import { useToast } from './ToastProvider'
import { useConfirm } from './ConfirmProvider'
import { RelinkChoiceModal } from './RelinkChoiceModal'
import { GeometryInfoButton } from './GeometryInfoModal'
import { EditorSection } from './object-editors/_shared'
import { ObjectRoleSection } from './ObjectRoleSection'
import {
  CodeObjectEditor, CommonObjectEditor, FreeTextAreaEditor,
  ImageObjectEditor, MatrixObjectEditor, QuestionObjectEditor, TextObjectEditor,
} from './object-editors'
import { useObjectDraft } from './object-editors/useObjectDraft'
import type { Capture } from './object-editors/objectDraft'

interface Props {
  objectId: string
  pageId: string
  /** Owning variant — drives the "Special role" section's role-bindings query. */
  variantId: string
  onDeleted: () => void
  /** Lets the workspace warn before navigating away with unsaved edits. */
  onDirtyChange?: (dirty: boolean) => void
}

/**
 * Full editor for the selected object (DRAFT only).
 *
 * The whole object is edited in ONE local draft (geometry excluded):
 * every section mutates the draft on change/blur and nothing is persisted until
 * the single "Save". Save diffs the draft against the last saved snapshot and
 * replays the change through the existing granular nd.* endpoints, in dependency
 * order, stopping at the first failure. On any failure the object is refetched so
 * the baseline realigns to what is really persisted; the user's draft is kept so
 * a second Save completes only the residual (idempotent).
 */
export function ObjectEditPanel({ objectId, pageId, variantId, onDeleted, onDirtyChange }: Props) {
  const toast = useToast()
  const confirm = useConfirm()
  const qc = useQueryClient()

  const detailQ = useQuery({
    queryKey: ['page-object', objectId],
    queryFn: () => nd.getPageObject(objectId),
  })

  // Same key/params as ObjectRoleSection's query — react-query dedupes
  // it to a single fetch. Used only to gate the CODE "Include page number" toggle;
  // ObjectRoleSection keeps its own subscription untouched.
  const roleBindingsQ = useQuery({
    queryKey: ['role-bindings', variantId],
    queryFn: () => nd.getVariantRoleBindings(variantId),
    enabled: !!variantId,
  })

  const draftApi = useObjectDraft(detailQ.data, objectId)
  const [saving, setSaving] = useState(false)
  const [partialError, setPartialError] = useState(false)
  // A linked member saved fine here but the BE resync to the
  // other pages failed — keep Save actionable so a retry runs the resync alone.
  const [needsResync, setNeedsResync] = useState(false)

  // Surface dirtiness to the workspace (navigation guard) and clear it on unmount.
  useEffect(() => { onDirtyChange?.(draftApi.dirty) }, [draftApi.dirty, onDirtyChange])
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange])

  const deleteM = useMutation({
    mutationFn: () => nd.deletePageObject(objectId),
    onSuccess: () => {
      toast.success('Object deleted')
      void qc.invalidateQueries({ queryKey: ['page-objects', pageId] })
      void qc.invalidateQueries({ queryKey: ['variant-max-score'] })
      onDeleted()
    },
    onError: (err) => toast.error(err),
  })

  // Enable / relink / unlink sharing for an object. The same POST
  // /share (link=true) both promotes a never-shared object to a template and
  // re-attaches a detached group; DELETE /share (link=false) detaches. In every
  // case existing pages are untouched — only pages added from now on get a copy.
  // The BE owns replication/propagation, so we just invalidate the object + page
  // lists afterwards and let queries refetch.
  const shareM = useMutation({
    mutationFn: (link: boolean) =>
      link ? nd.shareObject(objectId) : nd.unshareObject(objectId),
    onSuccess: (_data, link) => {
      void qc.invalidateQueries({ queryKey: ['page-object', objectId] })
      void qc.invalidateQueries({ queryKey: ['page-objects', pageId] })
      void qc.invalidateQueries({ queryKey: ['variant-max-score'] })
      toast.success(
        link
          ? 'This object is now shown on all pages'
          : 'Unlinked — each page now keeps its own independent copy',
      )
    },
    onError: (err) => toast.error(err),
  })

  async function handleUnlink() {
    const ok = await confirm({
      title: 'Unlink from other pages',
      message:
        'This detaches every copy of this object. From now on each page keeps its own independent copy — moving, resizing, reordering or deleting one no longer affects the others.',
      confirmLabel: 'Unlink',
      danger: true,
    })
    if (ok) shareM.mutate(false)
  }

  // Relink a DETACHED object. When other detached copies of
  // its group still exist the user must choose RECONNECT (overwrite them from this
  // page) vs FRESH (start a new group); when none remain we relink directly.
  // `relinkSiblings` non-null drives the choice modal (and carries the count).
  const [relinkSiblings, setRelinkSiblings] = useState<number | null>(null)
  const [relinkChecking, setRelinkChecking] = useState(false)

  const relinkM = useMutation({
    mutationFn: (mode: RelinkMode) => nd.relinkObject(objectId, mode),
    onSuccess: () => {
      setRelinkSiblings(null)
      void qc.invalidateQueries({ queryKey: ['page-object', objectId] })
      void qc.invalidateQueries({ queryKey: ['page-objects', pageId] })
      void qc.invalidateQueries({ queryKey: ['variant-max-score'] })
      toast.success('This object is now shown on all pages')
    },
    onError: (err) => toast.error(err),
  })

  async function handleRelinkClick() {
    setRelinkChecking(true)
    try {
      const info = await nd.getRelinkInfo(objectId)
      if (info.detached_siblings > 0) setRelinkSiblings(info.detached_siblings)
      else relinkM.mutate('RECONNECT')   // no siblings to overwrite → relink directly
    } catch (err) {
      toast.error(err)
    } finally {
      setRelinkChecking(false)
    }
  }

  // Enable sharing for an object that has never been shared (shared_group_id ==
  // null): promote it to a template ("copy from now on"). Existing pages are NOT
  // copied to — only pages added later receive a linked copy.
  async function handleEnableShare() {
    const ok = await confirm({
      title: 'Show on all pages',
      message:
        'This object will be copied onto pages added from now on. Existing pages are left untouched — they do not receive a copy.',
      confirmLabel: 'Show on all pages',
    })
    if (ok) shareM.mutate(true)
  }

  async function handleDelete(typeLabel: string, shared: boolean) {
    const ok = await confirm({
      title: 'Delete object',
      message: shared
        ? 'This object is shown on all pages — deleting it removes it from every page. This cannot be undone.'
        : `Delete this ${typeLabel} object? This cannot be undone.`,
      confirmLabel: 'Delete',
      danger: true,
    })
    if (ok) deleteM.mutate()
  }

  function handleReset() {
    setPartialError(false)
    draftApi.actions.reset()
  }

  async function handleSave() {
    if (saving) return
    const plan = draftApi.plan
    // A linked member whose typed config moved must fan out to its siblings; a
    // pending needsResync lets an otherwise-empty Save retry that fan-out alone.
    const isLinked = !!(detailQ.data?.shared_across_pages && detailQ.data?.shared_group_id)
    const configChanged = draftApi.configChanged
    if (plan.length === 0 && !needsResync) return

    setSaving(true)
    setPartialError(false)

    let failed = false
    let firstError: unknown = null
    if (plan.length > 0) {
      const captures: Capture[] = []
      for (const op of plan) {
        try {
          const capture = await op.run()
          if (capture) captures.push(capture)
        } catch (err) {
          failed = true
          firstError = err
          break   // stop at the first failure — do not push further changes
        }
      }

      // Realign to server truth (the successful slice persisted; the rest didn't).
      let fresh: PageObjectDetail | null = null
      try { fresh = await nd.getPageObject(objectId) } catch { fresh = null }
      if (fresh) qc.setQueryData(['page-object', objectId], fresh)
      void qc.invalidateQueries({ queryKey: ['page-objects', pageId] })
      draftApi.commit(captures, fresh, failed)
    }

    // Resync: once the member saved cleanly AND its typed
    // config changed (or a prior resync still owes a retry), push it to the
    // linked siblings. The siblings live on other pages, so we invalidate the
    // page-objects query — switching page then shows the realigned copies.
    let resyncError: unknown = null
    if (!failed && isLinked && (configChanged || needsResync)) {
      try {
        await nd.resyncSharedObject(objectId)
        setNeedsResync(false)
        void qc.invalidateQueries({ queryKey: ['page-objects', pageId] })
      } catch (err) {
        resyncError = err
        setNeedsResync(true)
      }
    }

    // The variant's maximum score once, at the end of the flow: after the
    // save AND the fan-out to the linked copies, whether either failed or not.
    if (plan.length > 0 || needsResync) {
      void qc.invalidateQueries({ queryKey: ['variant-max-score'] })
    }

    setSaving(false)
    if (failed) {
      setPartialError(true)
      toast.error(firstError, 'Some changes could not be saved')
    } else if (resyncError) {
      toast.error(resyncError, 'Saved here, but the other pages were not updated')
    } else {
      toast.success('Changes saved')
    }
  }

  if (detailQ.isLoading || !draftApi.ready || !draftApi.draft) {
    return <div className="nd-panel-section"><p className="info-msg">Loading…</p></div>
  }
  if (detailQ.isError || !detailQ.data) {
    return <div className="nd-panel-section"><p className="error-msg">{normalizeApiError(detailQ.error)}</p></div>
  }

  const obj = detailQ.data
  const d = draftApi.draft
  const accent = OBJECT_TYPE_STYLE[obj.object_type].color
  const a = draftApi.actions
  // "Include page number" is enabled only while this CODE object (or a shared
  // sibling) holds VARIANT_IDENTIFIER.
  const eligibleForPageIndex = holdsVariantIdentifier(obj, roleBindingsQ.data ?? [])

  return (
    <div className="nd-panel-section">
      <div className="row mb-16" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
        <div className="row row-8">
          <span className="badge" style={{ background: accent, color: '#fff' }}>Edit · {obj.object_type}</span>
          <GeometryInfoButton obj={obj} />
        </div>
        <button className="btn btn-secondary btn-sm nd-danger-btn"
                disabled={deleteM.isPending}
                onClick={() => handleDelete(obj.object_type, obj.shared_across_pages)}>
          {deleteM.isPending ? 'Deleting…' : 'Delete'}
        </button>
      </div>

      {partialError && (
        <div className="nd-edit-banner" role="alert">
          Partial save: some changes were not saved. Press Save again to retry — only what is missing will be applied.
        </div>
      )}

      {needsResync && (
        <div className="nd-edit-banner" role="alert">
          Saved on this page, but the other pages were not updated — press Save again to sync.
        </div>
      )}

      <div className="stack stack-16">
        {obj.shared_across_pages && (
          <EditorSection title="Shared across pages">
            <p className="nd-muted nd-edit-hint">
              🔗 Linked: moving, resizing, reordering or deleting it applies to every page.
            </p>
            <button type="button" className="btn btn-secondary btn-sm" disabled={shareM.isPending} onClick={handleUnlink}>
              {shareM.isPending ? 'Unlinking…' : 'Unlink from other pages'}
            </button>
          </EditorSection>
        )}
        {!obj.shared_across_pages && obj.shared_group_id && (
          <EditorSection title="Shared across pages">
            <p className="nd-muted nd-edit-hint">
              Unlinked: each page keeps its own independent copy. Relink to share edits across all pages again.
            </p>
            <button type="button" className="btn btn-secondary btn-sm"
                    disabled={relinkChecking || relinkM.isPending} onClick={handleRelinkClick}>
              {relinkChecking || relinkM.isPending ? 'Relinking…' : 'Relink to all pages'}
            </button>
          </EditorSection>
        )}
        {!obj.shared_across_pages && !obj.shared_group_id && (
          <EditorSection title="Shared across pages">
            <p className="nd-muted nd-edit-hint">
              This object lives only on this page. Enable to copy it onto pages added from now on (existing pages stay untouched).
            </p>
            <button type="button" className="btn btn-secondary btn-sm" disabled={shareM.isPending} onClick={handleEnableShare}>
              {shareM.isPending ? 'Enabling…' : 'Show on all pages'}
            </button>
          </EditorSection>
        )}

        <ObjectRoleSection obj={obj} variantId={variantId} pageId={pageId} />

        <EditorSection title="Presentation">
          <CommonObjectEditor value={d.presentation} onChange={a.setPresentation} />
        </EditorSection>

        <EditorSection title="Type configuration">
          {d.text && <TextObjectEditor value={d.text} onChange={a.setText} />}
          {d.code && <CodeObjectEditor value={d.code} onChange={a.setCode} eligibleForPageIndex={eligibleForPageIndex} />}
          {d.freeText && <FreeTextAreaEditor value={d.freeText} onChange={a.setFreeText} />}
          {d.question && (
            <QuestionObjectEditor
              value={d.question}
              onConfig={a.setQuestionConfig}
              onAddOption={a.addOption}
              onUpdateOption={a.updateOption}
              onDeleteOption={a.deleteOption}
            />
          )}
          {d.matrix && (
            <MatrixObjectEditor
              value={d.matrix}
              onConfig={a.setMatrixConfig}
              onCounts={a.setMatrixCounts}
              onRowLabel={a.updateRowLabel}
              onColLabel={a.updateColLabel}
              onCells={a.setCells}
            />
          )}
          {d.image && (
            <ImageObjectEditor
              objectId={objectId}
              pageId={pageId}
              config={obj.image_config}
              value={d.image}
              onChange={a.setImage}
            />
          )}
        </EditorSection>

        <div className="nd-edit-footer">
          <button type="button" className="btn btn-primary btn-sm" disabled={saving || (!draftApi.dirty && !needsResync)} onClick={handleSave}>
            {saving ? 'Saving…' : needsResync && !draftApi.dirty ? 'Sync other pages' : 'Save'}
          </button>
          <button type="button" className="btn btn-secondary btn-sm" disabled={saving || !draftApi.dirty} onClick={handleReset}>
            Reset
          </button>
          {draftApi.dirty && <span className="nd-unsaved-badge">● Unsaved changes</span>}
        </div>
      </div>

      {relinkSiblings != null && (
        <RelinkChoiceModal
          detachedSiblings={relinkSiblings}
          busy={relinkM.isPending}
          onReconnect={() => relinkM.mutate('RECONNECT')}
          onFresh={() => relinkM.mutate('FRESH')}
          onCancel={() => setRelinkSiblings(null)}
        />
      )}
    </div>
  )
}
