// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState, type FormEvent } from 'react'
import type { FormVariantCreate } from '../api/domain'

interface Props {
  onSubmit: (data: FormVariantCreate) => void | Promise<void>
  onCancel: () => void
  submitting?: boolean
}

/** Create form for a FormVariant. root_key is optional (server defaults it). */
export function VariantForm({ onSubmit, onCancel, submitting = false }: Props) {
  const [rootKey, setRootKey] = useState('')
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')

  function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (!name.trim()) return
    void onSubmit({
      root_key: rootKey.trim() || null,
      name: name.trim(),
      description: description.trim() || null,
    })
  }

  return (
    <form onSubmit={handleSubmit} className="stack stack-12">
      <div className="form-group">
        <label htmlFor="v-root">Root key <span className="nd-muted">(optional)</span></label>
        <input id="v-root" value={rootKey} onChange={e => setRootKey(e.target.value)}
               placeholder="e.g. test-a (auto-generated if blank)" />
      </div>
      <div className="form-group">
        <label htmlFor="v-name">Name</label>
        <input id="v-name" required value={name} onChange={e => setName(e.target.value)}
               placeholder="e.g. Test A" />
      </div>
      <div className="form-group">
        <label htmlFor="v-desc">Description</label>
        <textarea id="v-desc" rows={2} value={description} onChange={e => setDescription(e.target.value)} />
      </div>
      <div className="row row-8">
        <button type="submit" className="btn btn-primary" disabled={submitting || !name.trim()}>
          {submitting ? 'Creating…' : 'Create variant'}
        </button>
        <button type="button" className="btn btn-secondary" onClick={onCancel} disabled={submitting}>
          Cancel
        </button>
      </div>
    </form>
  )
}
