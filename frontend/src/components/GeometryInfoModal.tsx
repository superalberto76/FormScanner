// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState } from 'react'
import { createPortal } from 'react-dom'
import type { PageObjectDetail } from '../api/domain'

/** Just the technical geometry fields hidden from the default panels. */
type Geometry = Pick<PageObjectDetail, 'x' | 'y' | 'width' | 'height' | 'z_index'>

/**
 * Opt-in, read-only access to an object's technical geometry (x/y/width/height/
 * z-index). These fields are noise for the end user, so they're hidden from the
 * default detail/edit panels; this small button reveals them on demand.
 *
 * Purely read-only — independent of editorial state, so it's used in both the
 * DRAFT edit panel and the read-only (PUBLISHED) detail panel.
 */
export function GeometryInfoButton({ obj }: { obj: Geometry }) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button
        type="button"
        className="btn btn-secondary btn-sm"
        onClick={() => setOpen(true)}
        title="Show technical data (geometry)"
      >
        ⓘ Technical data
      </button>
      {open && <GeometryInfoModal obj={obj} onClose={() => setOpen(false)} />}
    </>
  )
}

function GeometryInfoModal({ obj, onClose }: { obj: Geometry; onClose: () => void }) {
  // Portal to <body> so it covers the canvas overlays/handles.
  return createPortal(
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-box" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h2>Technical data</h2>
          <button type="button" className="btn btn-secondary btn-sm" onClick={onClose}>✕</button>
        </div>
        <p className="nd-muted mb-16">Geometry (read-only).</p>
        <dl className="detail-grid">
          <dt>x</dt><dd>{obj.x}</dd>
          <dt>y</dt><dd>{obj.y}</dd>
          <dt>width</dt><dd>{obj.width}</dd>
          <dt>height</dt><dd>{obj.height}</dd>
          <dt>z-index</dt><dd>{obj.z_index}</dd>
        </dl>
        <div className="row row-8" style={{ marginTop: 16 }}>
          <button type="button" className="btn btn-secondary" onClick={onClose}>Close</button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
