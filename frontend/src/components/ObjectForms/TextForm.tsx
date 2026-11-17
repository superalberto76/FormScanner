// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState, type FormEvent } from 'react'
import type { TextPageObjectCreate } from '../../api/domain'
import { DEFAULT_GEOMETRY, GeometryFields, geometryToPayload, type GeometryState } from './GeometryFields'

interface Props {
  onSubmit: (data: TextPageObjectCreate) => void
  submitting?: boolean
  disabled?: boolean
  initialGeometry?: Partial<GeometryState>
}

export function TextForm({ onSubmit, submitting = false, disabled = false, initialGeometry }: Props) {
  const [geo, setGeo] = useState<GeometryState>({ ...DEFAULT_GEOMETRY, ...initialGeometry })
  const [text, setText] = useState('')
  const [fontSize, setFontSize] = useState(12)
  const [bold, setBold] = useState(false)
  const [italic, setItalic] = useState(false)

  function submit(e: FormEvent) {
    e.preventDefault()
    onSubmit({ ...geometryToPayload(geo), config: { text, font_size: fontSize, bold, italic } })
  }

  return (
    <form onSubmit={submit} className="stack stack-12">
      <GeometryFields value={geo} onChange={setGeo} />
      <div className="form-group">
        <label>Text</label>
        <textarea rows={2} value={text} onChange={e => setText(e.target.value)} required />
      </div>
      <div className="nd-grid-2">
        <div className="form-group">
          <label>Font size</label>
          <input type="number" min={1} value={fontSize}
                 onChange={e => setFontSize(parseInt(e.target.value, 10) || 12)} />
        </div>
        <div className="form-group">
          <label>Style</label>
          <div className="row row-12">
            <label className="nd-check"><input type="checkbox" checked={bold} onChange={e => setBold(e.target.checked)} /> Bold</label>
            <label className="nd-check"><input type="checkbox" checked={italic} onChange={e => setItalic(e.target.checked)} /> Italic</label>
          </div>
        </div>
      </div>
      <button type="submit" className="btn btn-primary" disabled={disabled || submitting || !text.trim()}>
        {submitting ? 'Adding…' : 'Add TEXT object'}
      </button>
    </form>
  )
}
