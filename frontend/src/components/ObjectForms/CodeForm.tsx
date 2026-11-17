// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState, type FormEvent } from 'react'
import {
  CODE_ENCODINGS,
  type CodeEncoding,
  type CodePageObjectCreate,
  type CodeType,
} from '../../api/domain'
import { DEFAULT_GEOMETRY, GeometryFields, geometryToPayload, type GeometryState } from './GeometryFields'

interface Props {
  onSubmit: (data: CodePageObjectCreate) => void
  submitting?: boolean
  disabled?: boolean
  initialGeometry?: Partial<GeometryState>
}

export function CodeForm({ onSubmit, submitting = false, disabled = false, initialGeometry }: Props) {
  const [geo, setGeo] = useState<GeometryState>({ ...DEFAULT_GEOMETRY, width: 120, height: 120, ...initialGeometry })
  const [codeType, setCodeType] = useState<CodeType>('QR')
  // QR carries no encoding; BARCODE requires one (default CODE128). Kept in
  // sync with code_type so the payload always matches the backend contract.
  const [encoding, setEncoding] = useState<CodeEncoding | null>(null)
  const [value, setValue] = useState('')
  const [humanReadable, setHumanReadable] = useState(false)

  function changeCodeType(next: CodeType) {
    setCodeType(next)
    setEncoding(next === 'BARCODE' ? 'CODE128' : null)
  }

  function submit(e: FormEvent) {
    e.preventDefault()
    onSubmit({
      ...geometryToPayload(geo),
      config: {
        code_type: codeType,
        encoding: codeType === 'BARCODE' ? (encoding ?? 'CODE128') : null,
        value,
        human_readable: humanReadable,
      },
    })
  }

  return (
    <form onSubmit={submit} className="stack stack-12">
      <GeometryFields value={geo} onChange={setGeo} />
      <div className="form-group">
        <label>Code type</label>
        <select value={codeType} onChange={e => changeCodeType(e.target.value as CodeType)}>
          <option value="QR">QR</option>
          <option value="BARCODE">BARCODE</option>
        </select>
      </div>
      {codeType === 'BARCODE' && (
        <div className="form-group">
          <label>Encoding</label>
          <select value={encoding ?? 'CODE128'} onChange={e => setEncoding(e.target.value as CodeEncoding)}>
            {CODE_ENCODINGS.map(enc => <option key={enc} value={enc}>{enc}</option>)}
          </select>
        </div>
      )}
      <div className="form-group">
        <label>Value</label>
        <input value={value} onChange={e => setValue(e.target.value)} required
               placeholder="encoded value" />
      </div>
      <label className="nd-check">
        <input type="checkbox" checked={humanReadable} onChange={e => setHumanReadable(e.target.checked)} />
        {' '}Human readable
      </label>
      <button type="submit" className="btn btn-primary" disabled={disabled || submitting || !value.trim()}>
        {submitting ? 'Adding…' : 'Add CODE object'}
      </button>
    </form>
  )
}
