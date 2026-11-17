// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useState, type FormEvent } from 'react'
import {
  IMAGE_ACCEPT,
  IMAGE_MAX_BYTES,
  type ImageFit,
  type ImagePageObjectCreate,
} from '../../api/domain'
import { normalizeApiError } from '../../utils/apiError'
import { DEFAULT_GEOMETRY, GeometryFields, geometryToPayload, type GeometryState } from './GeometryFields'

interface Props {
  /** Receives the create payload (geometry + fit) and the chosen file. The
   *  caller creates the object then uploads the file (two steps). */
  onSubmit: (data: ImagePageObjectCreate, file: File) => void
  submitting?: boolean
  disabled?: boolean
  initialGeometry?: Partial<GeometryState>
  /** Server error from the create/upload mutation (e.g. 415/413/422). */
  error?: unknown
}

const ALLOWED = IMAGE_ACCEPT.split(',')

export function ImageForm({ onSubmit, submitting = false, disabled = false, initialGeometry, error }: Props) {
  const [geo, setGeo] = useState<GeometryState>({ ...DEFAULT_GEOMETRY, width: 200, height: 200, ...initialGeometry })
  const [fit, setFit] = useState<ImageFit>('contain')
  const [file, setFile] = useState<File | null>(null)
  const [localError, setLocalError] = useState<string | null>(null)
  const [previewUrl, setPreviewUrl] = useState<string | null>(null)

  // Local object-URL preview (revoked on change/unmount). An <img> renders the
  // file — even an SVG never executes script in this context.
  useEffect(() => {
    if (!file) { setPreviewUrl(null); return }
    const url = URL.createObjectURL(file)
    setPreviewUrl(url)
    return () => URL.revokeObjectURL(url)
  }, [file])

  function pick(f: File | null) {
    setLocalError(null)
    if (!f) { setFile(null); return }
    // Client pre-check only (BE is authoritative). Empty type → let the BE
    // decide rather than risk a false rejection.
    if (f.type && !ALLOWED.includes(f.type)) {
      setLocalError('Unsupported format. Use PNG, JPG, BMP or SVG.')
      setFile(null)
      return
    }
    if (f.size > IMAGE_MAX_BYTES) {
      setLocalError(`File too large (max ${(IMAGE_MAX_BYTES / (1024 * 1024)).toFixed(0)} MB).`)
      setFile(null)
      return
    }
    setFile(f)
  }

  function submit(e: FormEvent) {
    e.preventDefault()
    if (!file) { setLocalError('Choose an image first.'); return }
    onSubmit({ ...geometryToPayload(geo), config: { fit } }, file)
  }

  const shownError = localError ?? (error ? normalizeApiError(error) : null)

  return (
    <form onSubmit={submit} className="stack stack-12">
      <GeometryFields value={geo} onChange={setGeo} />
      <div className="form-group">
        <label>Image file</label>
        <input
          type="file"
          accept={IMAGE_ACCEPT}
          onChange={e => pick(e.target.files?.[0] ?? null)}
        />
        <p className="nd-muted">PNG, JPG, BMP or SVG · max {(IMAGE_MAX_BYTES / (1024 * 1024)).toFixed(0)} MB</p>
      </div>
      {previewUrl && (
        <div className="nd-image-preview">
          <img src={previewUrl} alt="Preview" style={{ maxWidth: '100%', maxHeight: 160, objectFit: 'contain' }} />
        </div>
      )}
      <div className="form-group">
        <label>Fit</label>
        <select value={fit} onChange={e => setFit(e.target.value as ImageFit)}>
          <option value="contain">Contain (whole image visible)</option>
          <option value="cover">Cover (fill the area)</option>
        </select>
      </div>
      {shownError && <p className="nd-edit-error">{shownError}</p>}
      <button type="submit" className="btn btn-primary" disabled={disabled || submitting || !file}>
        {submitting ? 'Adding…' : 'Add IMAGE object'}
      </button>
    </form>
  )
}
