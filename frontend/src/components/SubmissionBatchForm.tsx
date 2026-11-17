// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState, type FormEvent } from 'react'
import type { SubmissionBatchCreate } from '../api/domain'
import { normalizeApiError } from '../utils/apiError'

interface Props {
  onSubmit: (data: SubmissionBatchCreate) => void | Promise<void>
  onCancel: () => void
  submitting?: boolean
  /**
   * Creation error, rendered inline right here rather than as a global toast.
   * Deliberately unlike variant creation (which only toasts): a batch fails
   * mostly with a 409 (project no longer ACTIVE), and the user must keep what
   * they typed while they read why — so the panel stays open and untouched.
   */
  error?: unknown
}

/** Create form for a SubmissionBatch. Name is required, description optional. */
export function SubmissionBatchForm({ onSubmit, onCancel, submitting = false, error }: Props) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')

  function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (!name.trim()) return
    void onSubmit({
      name: name.trim(),
      description: description.trim() || null,
    })
  }

  return (
    <form onSubmit={handleSubmit} className="stack stack-12">
      {error != null && <p className="error-msg">{normalizeApiError(error)}</p>}
      <div className="form-group">
        <label htmlFor="b-name">Name</label>
        <input id="b-name" required value={name} onChange={e => setName(e.target.value)}
               placeholder="e.g. Morning session" />
      </div>
      <div className="form-group">
        <label htmlFor="b-desc">Description</label>
        <textarea id="b-desc" rows={2} value={description} onChange={e => setDescription(e.target.value)} />
      </div>
      <div className="row row-8">
        <button type="submit" className="btn btn-primary" disabled={submitting || !name.trim()}>
          {submitting ? 'Creating…' : 'Create batch'}
        </button>
        <button type="button" className="btn btn-secondary" onClick={onCancel} disabled={submitting}>
          Cancel
        </button>
      </div>
    </form>
  )
}
