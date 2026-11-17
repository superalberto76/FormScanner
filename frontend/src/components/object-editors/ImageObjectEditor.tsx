// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useMutation, useQueryClient } from '@tanstack/react-query'
import {
  IMAGE_ACCEPT, IMAGE_MAX_BYTES, nd,
  type ImageObjectConfigRead,
} from '../../api/domain'
import { useObjectImage } from '../../hooks/useObjectImage'
import { useToast } from '../ToastProvider'
import type { ImageDraft } from './objectDraft'

interface Props {
  objectId: string
  pageId: string
  /** Latest server config (filename / dimensions / has_image), refetched live. */
  config: ImageObjectConfigRead | undefined
  /** Draftable props (fit) — controlled, persisted by the single Save. */
  value: ImageDraft
  onChange: (patch: Partial<ImageDraft>) => void
}

/**
 * IMAGE config editor. ``fit`` is part of the single-Save
 * draft (controlled). The file replace is an OUT-OF-BAND action — it uploads
 * immediately and refreshes the preview, independent of Save.
 */
export function ImageObjectEditor({ objectId, pageId, config, value, onChange }: Props) {
  const toast = useToast()
  const qc = useQueryClient()
  const hasImage = !!config?.has_image
  const { url, isLoading } = useObjectImage(objectId, hasImage)

  const uploadM = useMutation({
    mutationFn: (file: File) => nd.uploadObjectImage(objectId, file),
    onSuccess: () => {
      toast.success('Image replaced')
      void qc.invalidateQueries({ queryKey: ['object-image', objectId] })
      void qc.invalidateQueries({ queryKey: ['page-object', objectId] })
      void qc.invalidateQueries({ queryKey: ['page-objects', pageId] })
    },
    onError: (err) => toast.error(err),
  })

  function pick(file: File | null) {
    if (!file) return
    if (file.type && !IMAGE_ACCEPT.split(',').includes(file.type)) {
      toast.error(new Error('Unsupported format. Use PNG, JPG, BMP or SVG.'))
      return
    }
    if (file.size > IMAGE_MAX_BYTES) {
      toast.error(new Error(`File too large (max ${(IMAGE_MAX_BYTES / (1024 * 1024)).toFixed(0)} MB).`))
      return
    }
    uploadM.mutate(file)
  }

  return (
    <div className="stack stack-8">
      <div className="nd-image-preview">
        {url ? (
          <img src={url} alt={config?.original_filename ?? 'Image'}
               style={{ maxWidth: '100%', maxHeight: 180, objectFit: 'contain' }} />
        ) : (
          <p className="nd-muted">{isLoading ? 'Loading image…' : 'No image uploaded yet.'}</p>
        )}
      </div>

      {config?.has_image && (
        <p className="nd-muted">
          {config.original_filename ?? 'image'}
          {config.width_px && config.height_px ? ` · ${config.width_px}×${config.height_px}px` : ''}
          {config.mime_type ? ` · ${config.mime_type}` : ''}
        </p>
      )}

      <div className="form-group">
        <label>{config?.has_image ? 'Replace image' : 'Upload image'}</label>
        <input type="file" accept={IMAGE_ACCEPT} disabled={uploadM.isPending}
               onChange={e => { pick(e.target.files?.[0] ?? null); e.target.value = '' }} />
        {uploadM.isPending && <p className="nd-muted">Uploading…</p>}
      </div>

      <div className="form-group">
        <label>Fit</label>
        <select value={value.fit} onChange={e => onChange({ fit: e.target.value as ImageDraft['fit'] })}>
          <option value="contain">Contain (whole image visible)</option>
          <option value="cover">Cover (fill the area)</option>
        </select>
      </div>
    </div>
  )
}
