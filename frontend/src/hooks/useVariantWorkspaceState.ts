// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useState } from 'react'
import type { FormVariantPageRead, PageObjectType } from '../api/domain'
import type { Rect } from '../utils/viewerCoords'

// Explicit editor modes. EDIT_OBJECT is not a stored mode — it's derived from
// "VIEW + an object is selected" (see effectiveModeLabel below).
export type ViewerMode =
  | 'VIEW' | 'ADD_TEXT' | 'ADD_CODE' | 'ADD_FREE_TEXT_AREA' | 'ADD_QUESTION' | 'ADD_MATRIX' | 'ADD_IMAGE'

export type AddMode = Exclude<ViewerMode, 'VIEW'>

// Faithful-preview audience: STUDENT shows blank circles/cells like a
// printed blank form; SOLUTION fills the correct ones, exactly like the SOLUTION
// PDF export. Default STUDENT. Mirrors the export's ExportMode values.
export type PreviewMode = 'STUDENT' | 'SOLUTION'

/** Map an ADD_* mode to the PageObjectType it creates. */
export const ADD_MODE_TYPE: Record<AddMode, PageObjectType> = {
  ADD_TEXT: 'TEXT',
  ADD_CODE: 'CODE',
  ADD_FREE_TEXT_AREA: 'FREE_TEXT_AREA',
  ADD_QUESTION: 'QUESTION',
  ADD_MATRIX: 'MATRIX',
  ADD_IMAGE: 'IMAGE',
}

export function isAddMode(mode: ViewerMode): mode is AddMode {
  return mode !== 'VIEW'
}

/** A drawn rectangle awaiting type-specific config in the create modal. */
export interface PendingCreate {
  type: PageObjectType
  rect: Rect
}

/** User-facing label for the current mode (adds the derived EDIT_OBJECT state). */
export function effectiveModeLabel(mode: ViewerMode, hasSelection: boolean): string {
  if (mode !== 'VIEW') return mode
  return hasSelection ? 'EDIT_OBJECT' : 'VIEW'
}

/**
 * Lightweight workspace state: editor mode + page/object selection + a pending
 * draw awaiting the create modal. Kept local and small (no global store).
 *
 * The same VariantWorkspacePage instance is reused across variant ids (e.g.
 * after clone navigation), so we explicitly reset transient state when
 * `variantId` changes rather than relying on a remount.
 */
export function useVariantWorkspaceState(variantId: string, pages: FormVariantPageRead[]) {
  const [mode, setModeRaw] = useState<ViewerMode>('VIEW')
  const [selectedPageId, setSelectedPageId] = useState<string | null>(null)
  const [selectedObjectId, setSelectedObjectId] = useState<string | null>(null)
  const [pendingCreate, setPendingCreate] = useState<PendingCreate | null>(null)
  // Faithful-preview audience. A view-only preference (always on, in
  // DRAFT and read-only), so it is NOT reset on a variant switch.
  const [previewMode, setPreviewMode] = useState<PreviewMode>('STUDENT')

  // Variant changed (e.g. clone → navigate): reset mode + transient selection.
  // The first-page effect re-selects a valid page once new pages arrive.
  useEffect(() => {
    setModeRaw('VIEW')
    setSelectedObjectId(null)
    setPendingCreate(null)
  }, [variantId])

  // Auto-select the first page; re-select if the current one disappears.
  useEffect(() => {
    if (pages.length === 0) { setSelectedPageId(null); return }
    if (!selectedPageId || !pages.some(p => p.id === selectedPageId)) {
      setSelectedPageId(pages[0].id)
    }
  }, [pages, selectedPageId])

  // Changing page clears the (now-invalid) object selection.
  useEffect(() => { setSelectedObjectId(null) }, [selectedPageId])

  // Entering a create mode clears any selection so handles don't linger.
  function setMode(next: ViewerMode) {
    setModeRaw(next)
    if (next !== 'VIEW') setSelectedObjectId(null)
  }

  return {
    mode, setMode,
    selectedPageId, setSelectedPageId,
    selectedObjectId, setSelectedObjectId,
    pendingCreate, setPendingCreate,
    previewMode, setPreviewMode,
  }
}
