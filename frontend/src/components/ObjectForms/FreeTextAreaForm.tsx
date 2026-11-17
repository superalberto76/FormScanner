// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState, type FormEvent } from 'react'
import type { FreeTextAreaPageObjectCreate } from '../../api/domain'
import { DEFAULT_GEOMETRY, GeometryFields, geometryToPayload, type GeometryState } from './GeometryFields'

interface Props {
  onSubmit: (data: FreeTextAreaPageObjectCreate) => void
  submitting?: boolean
  disabled?: boolean
  initialGeometry?: Partial<GeometryState>
}

export function FreeTextAreaForm({ onSubmit, submitting = false, disabled = false, initialGeometry }: Props) {
  const [geo, setGeo] = useState<GeometryState>({ ...DEFAULT_GEOMETRY, width: 300, height: 120, ...initialGeometry })
  const [placeholder, setPlaceholder] = useState('')

  function submit(e: FormEvent) {
    e.preventDefault()
    onSubmit({
      ...geometryToPayload(geo),
      config: { placeholder: placeholder.trim() || null },
    })
  }

  return (
    <form onSubmit={submit} className="stack stack-12">
      <GeometryFields value={geo} onChange={setGeo} />
      <div className="form-group">
        <label>Placeholder</label>
        <input value={placeholder} onChange={e => setPlaceholder(e.target.value)}
               placeholder="e.g. Write your answer here" />
      </div>
      <button type="submit" className="btn btn-primary" disabled={disabled || submitting}>
        {submitting ? 'Adding…' : 'Add FREE_TEXT_AREA object'}
      </button>
    </form>
  )
}
