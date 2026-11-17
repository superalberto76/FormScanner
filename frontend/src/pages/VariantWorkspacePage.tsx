// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useCallback, useEffect, useRef, useState, type DragEvent } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Layout } from '../components/Layout'
import { useConfirm } from '../components/ConfirmProvider'
import { VariantToolbar } from '../components/VariantToolbar'
import { VariantPageViewer } from '../components/VariantPageViewer'
import { ViewerModeToolbar, type ZOrderAction } from '../components/ViewerModeToolbar'
import { PageObjectList } from '../components/PageObjectList'
import { ObjectDetailPanel } from '../components/ObjectDetailPanel'
import { IdentificationSummary } from '../components/IdentificationSummary'
import { useToast } from '../components/ToastProvider'
import { ObjectEditPanel } from '../components/ObjectEditPanel'
import { ObjectCreateModal } from '../components/ObjectCreateModal'
import { normalizeApiError } from '../utils/apiError'
import { nd, type PageObjectListItem, type PageObjectUpdate } from '../api/domain'
import { resolvePageSize, type Rect } from '../utils/viewerCoords'
import {
  countPageRemovalImpact,
  pageRemovalLinkedChip,
  pageRemovalMessage,
  reorderByDrop,
  sameOrder,
} from '../utils/pageRail'
import { ADD_MODE_TYPE, isAddMode, useVariantWorkspaceState } from '../hooks/useVariantWorkspaceState'
import '../styles/variantWorkspace.css'

/** Visual stacking order: back-to-front by (z_index, created_at) — matches the BE. */
function visualOrder(objects: PageObjectListItem[]): PageObjectListItem[] {
  return [...objects].sort(
    (a, b) => a.z_index - b.z_index || a.created_at.localeCompare(b.created_at),
  )
}

function swapAt(arr: PageObjectListItem[], a: number, b: number): PageObjectListItem[] {
  const next = [...arr]
  const tmp = next[a]
  next[a] = next[b]
  next[b] = tmp
  return next
}

/**
 * Pure z-order recompute. Reorders siblings per the action, then
 * normalizes z_index to 0..n-1 over the resulting visual order and returns only
 * the objects whose z_index actually changed (id + new z_index) — so the caller
 * PATCHes the minimum set. This stays correct even when many objects share the
 * same z_index (the default is 0). Empty when the move is a no-op (already at the
 * edge) or the object is missing.
 */
function computeZOrderPatches(
  objects: PageObjectListItem[],
  selectedId: string,
  action: ZOrderAction,
): { id: string; z_index: number }[] {
  const ordered = visualOrder(objects)
  const i = ordered.findIndex(o => o.id === selectedId)
  if (i < 0) return []
  const last = ordered.length - 1

  let next: PageObjectListItem[]
  if (action === 'forward') {
    if (i >= last) return []
    next = swapAt(ordered, i, i + 1)
  } else if (action === 'backward') {
    if (i <= 0) return []
    next = swapAt(ordered, i, i - 1)
  } else if (action === 'front') {
    if (i >= last) return []
    next = [...ordered.slice(0, i), ...ordered.slice(i + 1), ordered[i]]
  } else {
    if (i <= 0) return []
    next = [ordered[i], ...ordered.slice(0, i), ...ordered.slice(i + 1)]
  }

  return next
    .map((o, idx) => ({ id: o.id, z_index: idx }))
    .filter(p => ordered.find(o => o.id === p.id)!.z_index !== p.z_index)
}

export function VariantWorkspacePage() {
  const { variantId = '' } = useParams()
  const qc = useQueryClient()
  const toast = useToast()
  const confirm = useConfirm()
  // Tracks whether the open object editor has unsaved local edits, so
  // switching object/page can warn before the per-object draft is discarded.
  const editorDirtyRef = useRef(false)
  const onEditorDirtyChange = useCallback((dirty: boolean) => { editorDirtyRef.current = dirty }, [])

  const variantQ = useQuery({
    queryKey: ['variant', variantId],
    queryFn: () => nd.getVariant(variantId),
    enabled: !!variantId,
  })
  const pages = variantQ.data?.pages ?? []

  const {
    mode, setMode,
    selectedPageId, setSelectedPageId,
    selectedObjectId, setSelectedObjectId,
    pendingCreate, setPendingCreate,
    previewMode, setPreviewMode,
  } = useVariantWorkspaceState(variantId, pages)

  const objectsQ = useQuery({
    queryKey: ['page-objects', selectedPageId],
    queryFn: () => nd.listPageObjects(selectedPageId as string),
    enabled: !!selectedPageId,
  })
  const objects = objectsQ.data ?? []

  // Snap toggles: independent, in-memory (no storage), session-scoped.
  const [snapToGrid, setSnapToGrid] = useState(false)
  const [snapToObject, setSnapToObject] = useState(false)

  // Page reorder via native HTML5 DnD (DRAFT only). dragPageId is the
  // page being dragged; dropIndicator marks where the insertion line is drawn
  // (over item `index`, on the `after` half or the before half).
  const [dragPageId, setDragPageId] = useState<string | null>(null)
  const [dropIndicator, setDropIndicator] = useState<{ index: number; after: boolean } | null>(null)

  // Derived before any early return so the hardening effects can depend on it.
  // The editor freezes when EITHER the variant or its parent project is frozen.
  const variantStatus = variantQ.data?.status ?? 'DRAFT'
  const projectStatus = variantQ.data?.form_project_status ?? 'DRAFT'
  const variantFrozen = variantStatus !== 'DRAFT'           // PUBLISHED/ARCHIVED variant
  const projectFrozen = projectStatus !== 'DRAFT'           // ACTIVE/CLOSED/ARCHIVED project
  const isReadOnly = variantFrozen || projectFrozen

  // Keep selection valid: clear it when the selected object no longer exists
  // (deleted, or a stale id after a page switch). Prevents a stale detail panel.
  useEffect(() => {
    if (objectsQ.isSuccess && selectedObjectId && !objects.some(o => o.id === selectedObjectId)) {
      setSelectedObjectId(null)
    }
  }, [objectsQ.isSuccess, objects, selectedObjectId, setSelectedObjectId])

  // Read-only variants (PUBLISHED/ARCHIVED) force VIEW mode — no lingering
  // ADD/draw state after a publish flips the workspace to read-only.
  useEffect(() => {
    if (isReadOnly && mode !== 'VIEW') setMode('VIEW')
  }, [isReadOnly, mode, setMode])

  // Geometry commit from move/resize → PATCH; keep selection + refresh.
  const geometryM = useMutation({
    mutationFn: (vars: { id: string; data: PageObjectUpdate }) => nd.updatePageObject(vars.id, vars.data),
    onSuccess: (_data, vars) => {
      void qc.invalidateQueries({ queryKey: ['page-objects', selectedPageId] })
      // Also refresh the moved object's detail so the side-panel editor re-seeds
      // with the geometry committed from the canvas. Without this the panel keeps
      // the pre-drag values and "Save geometry" would overwrite the move/resize.
      void qc.invalidateQueries({ queryKey: ['page-object', vars.id] })
    },
    onError: (err, vars) => {
      toast.error(err)
      // Refetch to snap the overlay (and panel) back to server truth on a failed commit.
      void qc.invalidateQueries({ queryKey: ['page-objects', selectedPageId] })
      void qc.invalidateQueries({ queryKey: ['page-object', vars.id] })
    },
  })

  // Add a blank A4 page to a DRAFT variant. On success, refresh the variant
  // (which re-fetches its embedded pages) and select the freshly created page.
  const addPageM = useMutation({
    mutationFn: () => nd.addVariantPage(variantId),
    onSuccess: (created) => {
      void qc.invalidateQueries({ queryKey: ['variant', variantId] })
      setSelectedPageId(created.id)
      toast.success(`Page ${created.page_index} added`)
    },
    onError: (err) => toast.error(err),
  })

  // Duplicate a page (DRAFT only). On success: refresh the variant (re-fetches the
  // ordered pages, with the copy now right after the source) and jump selection to
  // the new page (product: land on the duplicate). Non-destructive — no confirm.
  const duplicatePageM = useMutation({
    mutationFn: (pageId: string) => nd.duplicateVariantPage(variantId, pageId),
    onSuccess: (created) => {
      void qc.invalidateQueries({ queryKey: ['variant', variantId] })
      void qc.invalidateQueries({ queryKey: ['variant-max-score'] })
      setSelectedPageId(created.id)
      setSelectedObjectId(null)
      toast.success('Page duplicated')
    },
    onError: (err) => toast.error(err),
  })

  // Reorder pages (DRAFT only). On success the variant re-fetches with its pages
  // in the new order; on error we toast and re-invalidate to snap back to server
  // truth. Page ids are stable across a reorder, so selection stays valid.
  const reorderM = useMutation({
    mutationFn: (orderedIds: string[]) => nd.reorderVariantPages(variantId, orderedIds),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['variant', variantId] })
      // The first linked copy counts for the maximum score: should the
      // copies be out of sync, a new page order can change which one that is.
      void qc.invalidateQueries({ queryKey: ['variant-max-score'] })
      toast.success('Page order updated')
    },
    onError: (err) => {
      toast.error(err)
      void qc.invalidateQueries({ queryKey: ['variant', variantId] })
    },
  })

  // Remove a page (DRAFT only). On success: refresh the variant, drop the removed
  // page's object cache, and if the removed page was selected move selection to
  // the first remaining page (clearing the object selection).
  const deletePageM = useMutation({
    mutationFn: (pageId: string) => nd.deleteVariantPage(variantId, pageId),
    onSuccess: (remaining, pageId) => {
      void qc.invalidateQueries({ queryKey: ['variant', variantId] })
      void qc.invalidateQueries({ queryKey: ['variant-max-score'] })
      qc.removeQueries({ queryKey: ['page-objects', pageId] })
      if (pageId === selectedPageId) {
        setSelectedPageId(remaining[0]?.id ?? null)
        setSelectedObjectId(null)
      }
      toast.success('Page removed')
    },
    onError: (err) => toast.error(err),
  })

  if (variantQ.isLoading) return <Layout><p className="info-msg">Loading variant…</p></Layout>
  if (variantQ.isError || !variantQ.data) {
    return (
      <Layout>
        <div className="card" style={{ textAlign: 'center' }}>
          <h1 className="mb-16">Variant unavailable</h1>
          <p className="error-msg mb-16">
            {normalizeApiError(variantQ.error, 'This variant could not be loaded.')}
          </p>
          <Link to="/projects" className="btn btn-primary">← Back to projects</Link>
        </div>
      </Layout>
    )
  }

  const variant = variantQ.data
  const readOnly = isReadOnly
  const selectedPage = pages.find(p => p.id === selectedPageId) ?? null
  const objectsErrorText = objectsQ.isError ? normalizeApiError(objectsQ.error) : undefined

  // Stacking bounds for the selected object (drives z-order button enablement).
  const zOrdered = visualOrder(objects)
  const selZIndex = selectedObjectId ? zOrdered.findIndex(o => o.id === selectedObjectId) : -1
  const canBringForward = selZIndex >= 0 && selZIndex < zOrdered.length - 1
  const canSendBackward = selZIndex > 0

  function handleZOrderChange(action: ZOrderAction) {
    if (!selectedObjectId || readOnly) return
    // Reuse the geometry mutation pattern: per-object PATCH of the new z_index,
    // each invalidating ['page-objects', selectedPageId] so the viewer re-stacks.
    for (const p of computeZOrderPatches(objects, selectedObjectId, action)) {
      geometryM.mutate({ id: p.id, data: { z_index: p.z_index } })
    }
  }

  function handleDrawComplete(rect: Rect) {
    if (!isAddMode(mode)) return
    setPendingCreate({ type: ADD_MODE_TYPE[mode], rect })
  }

  function handleGeometryCommit(id: string, rect: Rect) {
    // Defensive second line of defence: never PATCH geometry of a locked object,
    // even if a drag/resize somehow reached this point. The viewer already
    // disarms move/resize for locked overlays (see PageOverlay).
    if (objects.find(o => o.id === id)?.locked) return
    geometryM.mutate({ id, data: { x: rect.x, y: rect.y, width: rect.width, height: rect.height } })
  }

  // Warn before discarding unsaved object edits. Switching object or
  // page remounts/closes the editor and drops its local draft, so confirm first.
  async function confirmDiscardIfDirty(): Promise<boolean> {
    if (!editorDirtyRef.current) return true
    const ok = await confirm({
      title: 'Unsaved changes',
      message: 'This object has unsaved changes. Discard them?',
      confirmLabel: 'Discard', danger: true,
    })
    if (ok) editorDirtyRef.current = false
    return ok
  }
  async function selectObject(id: string | null) {
    if (id === selectedObjectId) return
    if (await confirmDiscardIfDirty()) setSelectedObjectId(id)
  }
  async function selectPage(id: string) {
    if (id === selectedPageId) return
    if (await confirmDiscardIfDirty()) setSelectedPageId(id)
  }

  // ── Page rail drag-reorder (DRAFT only) ─────────────────────────────────────
  function handlePageDragStart(e: DragEvent<HTMLDivElement>, pageId: string) {
    if (readOnly) return
    setDragPageId(pageId)
    e.dataTransfer.effectAllowed = 'move'
    e.dataTransfer.setData('text/plain', pageId) // Firefox needs data to start a drag
  }
  function handlePageDragOver(e: DragEvent<HTMLDivElement>, index: number) {
    if (readOnly || !dragPageId) return
    e.preventDefault()
    e.dataTransfer.dropEffect = 'move'
    const r = e.currentTarget.getBoundingClientRect()
    setDropIndicator({ index, after: e.clientY > r.top + r.height / 2 })
  }
  function handlePageDragEnd() {
    setDragPageId(null)
    setDropIndicator(null)
  }
  function handlePageDrop(e: DragEvent<HTMLDivElement>) {
    if (readOnly || !dragPageId || !dropIndicator) { handlePageDragEnd(); return }
    e.preventDefault()
    const slot = dropIndicator.after ? dropIndicator.index + 1 : dropIndicator.index
    const ids = pages.map(p => p.id)
    const next = reorderByDrop(ids, dragPageId, slot)
    handlePageDragEnd()
    if (!sameOrder(next, ids)) reorderM.mutate(next)
  }

  // ── Page removal (DRAFT only) ────────────────────────────────────────────────
  async function handleRemovePage(pageId: string) {
    if (readOnly || pages.length <= 1) return
    // Fetch THAT page's objects on demand (served from cache when already loaded)
    // to compute N (total objects) and M (linked "show on all pages" members).
    let objs: PageObjectListItem[]
    try {
      objs = await qc.fetchQuery({
        queryKey: ['page-objects', pageId],
        queryFn: () => nd.listPageObjects(pageId),
      })
    } catch (err) {
      toast.error(err)
      return
    }
    const impact = countPageRemovalImpact(objs)
    const chip = pageRemovalLinkedChip(impact.linked)
    const ok = await confirm({
      title: 'Remove page',
      danger: true,
      confirmLabel: 'Remove',
      message: pageRemovalMessage(impact),
      details: chip ? <span className="nd-removal-chip">{chip}</span> : undefined,
    })
    if (ok) deletePageM.mutate(pageId)
  }

  return (
    <Layout>
      <p className="nd-breadcrumb mb-16">
        <Link to="/projects">Projects</Link> <span className="nd-muted">/</span>{' '}
        <Link to={`/projects/${variant.form_project_id}`}>Project</Link>{' '}
        <span className="nd-muted">/</span> {variant.name}
      </p>

      <div className="workspace-card">
        <VariantToolbar variant={variant} />

        {readOnly && (
          <div style={{ padding: '10px 16px' }}>
            <div className="readonly-banner">
              {variantFrozen
                ? `This variant is ${variant.status}. The editor is read-only — clone it to make changes. Export is still available.`
                : `The project is ${projectStatus}. The editor is read-only — reopen the project (→ DRAFT) to edit. Export is still available.`}
            </div>
          </div>
        )}

        {!readOnly && (
          <ViewerModeToolbar
            mode={mode}
            hasSelection={!!selectedObjectId}
            readOnly={readOnly}
            onChangeMode={setMode}
            onZOrder={handleZOrderChange}
            canForward={canBringForward}
            canBackward={canSendBackward}
            snapToGrid={snapToGrid}
            onToggleSnapGrid={() => setSnapToGrid(v => !v)}
            snapToObject={snapToObject}
            onToggleSnapObject={() => setSnapToObject(v => !v)}
          />
        )}

        <div className="nd-workspace-body">
          {/* Left: pages */}
          <aside className="nd-pages-rail">
            <div className="workspace-pages-title">Pages</div>
            {pages.length === 0 && <p className="info-msg">No pages.</p>}
            {pages.map((p, index) => {
              const dropBefore = dropIndicator?.index === index && !dropIndicator.after
              const dropAfter = dropIndicator?.index === index && dropIndicator.after
              return (
                <div
                  key={p.id}
                  className={[
                    'nd-page-item',
                    p.id === selectedPageId ? 'is-active' : '',
                    dragPageId === p.id ? 'is-dragging' : '',
                    dropBefore ? 'is-drop-before' : '',
                    dropAfter ? 'is-drop-after' : '',
                  ].filter(Boolean).join(' ')}
                  draggable={!readOnly}
                  onDragStart={(e) => handlePageDragStart(e, p.id)}
                  onDragOver={(e) => handlePageDragOver(e, index)}
                  onDrop={handlePageDrop}
                  onDragEnd={handlePageDragEnd}
                >
                  {!readOnly && (
                    <span className="nd-page-drag-handle" aria-hidden="true" title="Drag to reorder">⠿</span>
                  )}
                  <button
                    type="button"
                    className="nd-page-label"
                    onClick={() => void selectPage(p.id)}
                  >
                    Page {p.page_index}
                  </button>
                  {!readOnly && (
                    <button
                      type="button"
                      className="nd-page-duplicate"
                      disabled={duplicatePageM.isPending}
                      onClick={(e) => { e.stopPropagation(); duplicatePageM.mutate(p.id) }}
                      title="Duplicate page"
                      aria-label={`Duplicate page ${p.page_index}`}
                    >
                      ⧉
                    </button>
                  )}
                  {!readOnly && (
                    <button
                      type="button"
                      className="nd-page-trash"
                      disabled={pages.length <= 1 || deletePageM.isPending}
                      onClick={() => void handleRemovePage(p.id)}
                      title="Remove page"
                      aria-label={`Remove page ${p.page_index}`}
                    >
                      🗑
                    </button>
                  )}
                </div>
              )
            })}
            {!readOnly && pages.length <= 1 && (
              <p className="nd-page-rail-note">
                Removal disabled: a variant always has at least one page
              </p>
            )}
            {!readOnly && (
              <button
                className="btn btn-secondary btn-sm nd-page-add"
                onClick={() => addPageM.mutate()}
                disabled={addPageM.isPending}
              >
                {addPageM.isPending ? 'Adding…' : '+ Add page'}
              </button>
            )}
          </aside>

          {/* Center: viewer + editor */}
          <section className="nd-workspace-center">
            {/* Faithful-preview audience selector. Always visible — in
                DRAFT AND read-only — so it sits here, above the viewer, rather than
                inside ViewerModeToolbar (which is hidden in read-only). */}
            <div className="nd-preview-mode" role="group" aria-label="Preview audience">
              <span className="nd-preview-mode-label">Preview</span>
              <div className="nd-preview-mode-seg">
                <button
                  type="button"
                  className={`nd-preview-mode-btn${previewMode === 'STUDENT' ? ' is-active' : ''}`}
                  aria-pressed={previewMode === 'STUDENT'}
                  onClick={() => setPreviewMode('STUDENT')}
                >
                  Student
                </button>
                <button
                  type="button"
                  className={`nd-preview-mode-btn${previewMode === 'SOLUTION' ? ' is-active' : ''}`}
                  aria-pressed={previewMode === 'SOLUTION'}
                  onClick={() => setPreviewMode('SOLUTION')}
                >
                  Solution
                </button>
              </div>
            </div>
            {selectedPage ? (
              <VariantPageViewer
                page={selectedPage}
                objects={objects}
                mode={mode}
                previewMode={previewMode}
                readOnly={readOnly}
                isLoading={objectsQ.isLoading}
                isError={objectsQ.isError}
                errorText={objectsErrorText}
                selectedObjectId={selectedObjectId}
                onSelectObject={(id) => void selectObject(id)}
                onDrawComplete={handleDrawComplete}
                onGeometryCommit={handleGeometryCommit}
                snapToGrid={snapToGrid}
                snapToObject={snapToObject}
              />
            ) : (
              <p className="info-msg">Select a page.</p>
            )}
          </section>

          {/* Right: identification summary (always visible) + edit (DRAFT) or
              detail (read-only) + object list */}
          <aside className="nd-workspace-panel">
            <IdentificationSummary variantId={variantId} />
            {selectedObjectId && selectedPage && !readOnly ? (
              <ObjectEditPanel
                key={selectedObjectId}
                objectId={selectedObjectId}
                pageId={selectedPage.id}
                variantId={variantId}
                onDeleted={() => setSelectedObjectId(null)}
                onDirtyChange={onEditorDirtyChange}
              />
            ) : (
              <ObjectDetailPanel objectId={selectedObjectId} />
            )}
            {selectedPage && (
              <PageObjectList
                objects={objects}
                isLoading={objectsQ.isLoading}
                isError={objectsQ.isError}
                errorText={objectsErrorText}
                selectedObjectId={selectedObjectId}
                onSelect={(id) => void selectObject(id)}
                bandCheckPage={readOnly ? null : resolvePageSize(selectedPage)}
              />
            )}
          </aside>
        </div>
      </div>

      {pendingCreate && selectedPage && (
        <ObjectCreateModal
          pageId={selectedPage.id}
          type={pendingCreate.type}
          rect={pendingCreate.rect}
          onClose={() => { setPendingCreate(null); setMode('VIEW') }}
          onCreated={(id) => { setSelectedObjectId(id); setMode('VIEW') }}
        />
      )}
    </Layout>
  )
}
