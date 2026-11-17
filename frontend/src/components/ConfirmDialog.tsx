// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState, useEffect, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { describeError } from '../api/errors'

interface Props {
  title: string
  message: ReactNode
  /** Optional block rendered below the message (e.g. a chip/note). */
  details?: ReactNode
  /** Label for the confirming action button (default "Confirm"). */
  confirmLabel?: string
  /** When true, the confirm button is styled as destructive. */
  danger?: boolean
  /** May be async — the dialog shows a busy state and surfaces thrown errors. */
  onConfirm: () => void | Promise<void>
  onCancel: () => void
}

/**
 * Lightweight reusable confirmation modal. Used instead of window.confirm so async
 * failures (e.g. a 404 because the entity was already deleted) can be shown
 * inline instead of silently swallowed.
 */
export function ConfirmDialog({
  title, message, details, confirmLabel = 'Confirm', danger = false, onConfirm, onCancel,
}: Props) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  // Escape cancels the dialog (unless an action is in flight).
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape' && !busy) onCancel()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [busy, onCancel])

  async function handleConfirm() {
    setBusy(true); setError('')
    try {
      await onConfirm()
    } catch (err) {
      setError(describeError(err, 'Action failed'))
      setBusy(false)
    }
  }

  // Portal to <body> so the backdrop escapes any workspace stacking context and
  // covers the canvas overlays/handles.
  return createPortal(
    <div className="modal-backdrop" onClick={busy ? undefined : onCancel}>
      <div className="modal-box" style={{ width: 380 }} onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h2>{title}</h2>
        </div>
        <div className="stack stack-12">
          <p className="info-msg" style={{ color: 'var(--text)' }}>{message}</p>
          {details && <div className="stack stack-8">{details}</div>}
          {error && <p className="error-msg">{error}</p>}
          <div className="row row-8">
            <button
              className="btn btn-primary"
              style={danger ? { background: 'var(--danger)' } : undefined}
              onClick={handleConfirm}
              disabled={busy}
            >
              {busy ? 'Working…' : confirmLabel}
            </button>
            <button className="btn btn-secondary" onClick={onCancel} disabled={busy}>
              Cancel
            </button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  )
}
