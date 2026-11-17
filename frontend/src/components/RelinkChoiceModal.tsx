// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { createPortal } from 'react-dom'

interface Props {
  /** How many still-detached copies of the group exist (drives the wording). */
  detachedSiblings: number
  busy?: boolean
  /** Yes — re-link the detached siblings and overwrite them from this page. */
  onReconnect: () => void
  /** No — start a new link; leave the existing copies independent. */
  onFresh: () => void
  onCancel: () => void
}

/**
 * Yes/no choice when reactivating sharing on a detached object that still has
 * detached copies on other pages. Rendered in a portal so it
 * sits above the canvas overlays, like ObjectCreateModal.
 */
export function RelinkChoiceModal({ detachedSiblings, busy = false, onReconnect, onFresh, onCancel }: Props) {
  const n = detachedSiblings
  const copies = `${n} other ${n === 1 ? 'page' : 'pages'}`

  return createPortal(
    <div className="modal-backdrop" onClick={busy ? undefined : onCancel}>
      <div className="modal-box" style={{ width: 480, maxWidth: '92vw' }} onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h2>Show on all pages</h2>
          <button className="btn btn-secondary btn-sm" onClick={onCancel} disabled={busy}>✕</button>
        </div>

        <p className="nd-muted mb-16">
          This object has independent copies on {copies}. Choose how to reactivate sharing — neither
          option adds the object to pages that don't already have a copy.
        </p>

        <div className="stack stack-12">
          <div className="nd-panel-section">
            <strong>Reconnect &amp; overwrite</strong>
            <p className="nd-muted nd-edit-hint">
              The copies on the other pages lose their local changes and are replaced with this page's version.
            </p>
            <button type="button" className="btn btn-primary btn-sm" onClick={onReconnect} disabled={busy}>
              {busy ? 'Working…' : 'Reconnect & overwrite'}
            </button>
          </div>

          <div className="nd-panel-section">
            <strong>Create a new link</strong>
            <p className="nd-muted nd-edit-hint">
              The existing copies stay independent; this object is copied only onto pages added from now on.
            </p>
            <button type="button" className="btn btn-secondary btn-sm" onClick={onFresh} disabled={busy}>
              {busy ? 'Working…' : 'Create a new link'}
            </button>
          </div>
        </div>

        <div className="nd-edit-footer">
          <button type="button" className="btn btn-secondary btn-sm" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
