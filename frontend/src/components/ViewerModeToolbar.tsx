// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useRef, useState } from 'react'
import type { ViewerMode } from '../hooks/useVariantWorkspaceState'
import { effectiveModeLabel } from '../hooks/useVariantWorkspaceState'

/** PowerPoint-style stacking actions on the selected object. */
export type ZOrderAction = 'forward' | 'backward' | 'front' | 'back'

interface Props {
  mode: ViewerMode
  hasSelection: boolean
  readOnly: boolean
  onChangeMode: (mode: ViewerMode) => void
  /** Stacking-order controls for the selected object (DRAFT only). */
  onZOrder: (action: ZOrderAction) => void
  /** False when the selected object is already on top (disables forward/front). */
  canForward: boolean
  /** False when the selected object is already at the bottom (disables backward/back). */
  canBackward: boolean
  /** Snap-to-grid toggle — latch edits to a 5px grid + show it. */
  snapToGrid: boolean
  onToggleSnapGrid: () => void
  /** Snap-to-object toggle — latch edges/centres to other objects. */
  snapToObject: boolean
  onToggleSnapObject: () => void
}

const Z_ORDER_OPTIONS: { action: ZOrderAction; label: string; up: boolean }[] = [
  { action: 'front', label: '⤒ Bring to front', up: true },
  { action: 'forward', label: '↑ Bring forward', up: true },
  { action: 'backward', label: '↓ Send backward', up: false },
  { action: 'back', label: '⤓ Send to back', up: false },
]

// Simple monochrome glyphs evoking each object type (toolbar dropdown).
const ADD_TOOLS: { mode: ViewerMode; label: string }[] = [
  { mode: 'ADD_TEXT', label: 'T Text' },
  { mode: 'ADD_CODE', label: '▤ Code' },
  { mode: 'ADD_FREE_TEXT_AREA', label: '¶ Free text' },
  { mode: 'ADD_QUESTION', label: '◉ Question' },
  { mode: 'ADD_MATRIX', label: '▦ Matrix' },
  { mode: 'ADD_IMAGE', label: '🖼 Image' },
]

/** Mode selector + always-visible current-mode indicator. */
export function ViewerModeToolbar({
  mode, hasSelection, readOnly, onChangeMode, onZOrder, canForward, canBackward,
  snapToGrid, onToggleSnapGrid, snapToObject, onToggleSnapObject,
}: Props) {
  if (readOnly) {
    return (
      <div className="nd-mode-toolbar">
        <span className="nd-mode-indicator">Mode: <strong>VIEW</strong> (read-only)</span>
      </div>
    )
  }
  return (
    <div className="nd-mode-toolbar">
      <button
        className={`btn btn-sm ${mode === 'VIEW' ? 'btn-primary' : 'btn-secondary'}`}
        onClick={() => onChangeMode('VIEW')}
      >
        Select / Edit
      </button>
      <select
        className="nd-toolbar-select"
        value=""
        title="Add an object — pick a type, then drag on the page to draw it"
        onChange={e => { if (e.target.value) onChangeMode(e.target.value as ViewerMode) }}
      >
        <option value="" disabled hidden>Add object</option>
        {ADD_TOOLS.map(t => (
          <option key={t.mode} value={t.mode}>{t.label}</option>
        ))}
      </select>
      {/* Always visible in DRAFT; disabled with no selection or when no move is
          possible (object alone / already at both edges). */}
      <select
        className="nd-toolbar-select"
        value=""
        disabled={!hasSelection || (!canForward && !canBackward)}
        title={hasSelection ? 'Change the stacking order of the selected object' : 'Select an object to change its stacking order'}
        onChange={e => { if (e.target.value) onZOrder(e.target.value as ZOrderAction) }}
      >
        <option value="" disabled hidden>Sort objects</option>
        {Z_ORDER_OPTIONS.map(o => (
          <option key={o.action} value={o.action} disabled={o.up ? !canForward : !canBackward}>
            {o.label}
          </option>
        ))}
      </select>
      {/* Snap dropdown — one menu, two independent options (one,
          the other, or both). DRAFT-only. */}
      <SnapMenu
        snapToGrid={snapToGrid}
        onToggleSnapGrid={onToggleSnapGrid}
        snapToObject={snapToObject}
        onToggleSnapObject={onToggleSnapObject}
      />
      <span className="nd-mode-indicator">
        Mode: <strong>{effectiveModeLabel(mode, hasSelection)}</strong>
        {mode !== 'VIEW' && <span className="nd-muted"> · drag on the page to draw</span>}
      </span>
    </div>
  )
}

interface SnapMenuProps {
  snapToGrid: boolean
  onToggleSnapGrid: () => void
  snapToObject: boolean
  onToggleSnapObject: () => void
}

/**
 * Single "Snap" dropdown holding the two independent snap options (grid /
 * object). Either, both, or neither can be checked; toggling one keeps the menu
 * open so both can be set in one go. Closes on outside-click or Escape.
 */
function SnapMenu({ snapToGrid, onToggleSnapGrid, snapToObject, onToggleSnapObject }: SnapMenuProps) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const activeCount = (snapToGrid ? 1 : 0) + (snapToObject ? 1 : 0)

  useEffect(() => {
    if (!open) return
    function onPointerDown(e: PointerEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  return (
    <div className="nd-snap-menu" ref={ref}>
      {/* Looks like the two native <select>s (shares .nd-toolbar-select) but stays
          a <button>: it drives two independent toggles via a popover, which a
          native <select> can't express. Active state is a subtle accent, never a
          solid primary fill, so the three controls read as one family. */}
      <button
        type="button"
        className={`nd-toolbar-select nd-snap-trigger${activeCount ? ' is-active' : ''}`}
        aria-haspopup="true"
        aria-expanded={open}
        title="Snapping — choose grid, object, or both"
        onClick={() => setOpen(o => !o)}
      >
        <span>Snap</span>
        {activeCount > 0 && <span className="nd-snap-count">{activeCount}</span>}
      </button>
      {open && (
        <div className="nd-snap-popover" role="menu">
          <label className="nd-snap-item">
            <input type="checkbox" checked={snapToGrid} onChange={onToggleSnapGrid} />
            <span>Snap to grid</span>
          </label>
          <label className="nd-snap-item">
            <input type="checkbox" checked={snapToObject} onChange={onToggleSnapObject} />
            <span>Snap to object</span>
          </label>
        </div>
      )}
    </div>
  )
}
