// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { type ReactNode, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { normalizeApiError } from '../utils/apiError'
import { OBJECT_TYPE_STYLE } from '../utils/objectDisplay'
import {
  DASH, fmt, labelsByOrder, textStyleSummary, yesNo,
} from '../utils/objectDetailFormat'
import {
  nd,
  type CodeObjectConfigRead,
  type FreeTextAreaConfigRead,
  type ImageObjectConfigRead,
  type MatrixObjectConfigRead,
  type PageObjectDetail,
  type QuestionObjectConfigRead,
  type TextObjectConfigRead,
} from '../api/domain'
import { useObjectImage } from '../hooks/useObjectImage'
import { EditCellsModal } from './EditCellsModal'
import { GeometryInfoButton } from './GeometryInfoModal'
import { useToast } from './ToastProvider'

interface Props {
  objectId: string | null
}

/**
 * Read-only details for the selected PageObject. Fetches the full typed read
 * (GET /page-objects/{id}); the list/overlay only carry base fields.
 *
 * Rendering is split into small focused sections (CommonFields + one per typed
 * config) and value formatting is delegated to utils/objectDetailFormat.
 */
export function ObjectDetailPanel({ objectId }: Props) {
  const detailQ = useQuery({
    queryKey: ['page-object', objectId],
    queryFn: () => nd.getPageObject(objectId as string),
    enabled: !!objectId,
  })

  return (
    <div className="nd-panel-section">
      <h3 className="mb-16">Object details</h3>
      {!objectId && <p className="info-msg">Select an object on the page or in the list.</p>}
      {objectId && detailQ.isLoading && <p className="info-msg">Loading…</p>}
      {objectId && detailQ.isError && <p className="error-msg">{normalizeApiError(detailQ.error)}</p>}
      {objectId && detailQ.data && <DetailBody obj={detailQ.data} />}
    </div>
  )
}

// ── primitives ───────────────────────────────────────────────────────────────

/** One labelled row inside a `.detail-grid`. */
function Field({ label, children }: { label: string; children: ReactNode }) {
  return (<><dt>{label}</dt><dd>{children}</dd></>)
}

/** Small copy-to-clipboard button with toast feedback. */
function CopyButton({ text, what }: { text: string; what: string }) {
  const toast = useToast()
  async function onCopy() {
    try {
      if (!navigator.clipboard) throw new Error('clipboard unavailable')
      await navigator.clipboard.writeText(text)
      toast.success(`${what} copied`)
    } catch {
      toast.error(`Could not copy ${what.toLowerCase()}`)
    }
  }
  return (
    <button type="button" className="btn btn-secondary nd-copy-btn" onClick={onCopy} title={`Copy ${what.toLowerCase()}`}>
      Copy
    </button>
  )
}

// ── sections ─────────────────────────────────────────────────────────────────

function DetailBody({ obj }: { obj: PageObjectDetail }) {
  return (
    <div className="stack stack-12">
      <CommonFields obj={obj} />
      <div className="divider" />
      {obj.text_config && <TextObjectDetailSection cfg={obj.text_config} />}
      {obj.code_config && <CodeObjectDetailSection cfg={obj.code_config} />}
      {obj.free_text_config && <FreeTextAreaDetailSection cfg={obj.free_text_config} />}
      {obj.question_config && <QuestionObjectDetailSection cfg={obj.question_config} />}
      {obj.matrix_config && <MatrixObjectDetailSection cfg={obj.matrix_config} objectId={obj.id} />}
      {obj.image_config && <ImageObjectDetailSection cfg={obj.image_config} objectId={obj.id} />}
    </div>
  )
}

function ImageObjectDetailSection({ cfg, objectId }: { cfg: ImageObjectConfigRead; objectId: string }) {
  const { url, isLoading } = useObjectImage(objectId, cfg.has_image)
  return (
    <div className="stack stack-8">
      <div className="nd-image-preview">
        {url ? (
          <img src={url} alt={cfg.original_filename ?? 'Image'}
               style={{ maxWidth: '100%', maxHeight: 180, objectFit: 'contain' }} />
        ) : (
          <p className="nd-muted">{isLoading ? 'Loading image…' : 'No image uploaded.'}</p>
        )}
      </div>
      <dl className="detail-grid">
        <Field label="File">{fmt(cfg.original_filename)}</Field>
        <Field label="Type">{fmt(cfg.mime_type)}</Field>
        <Field label="Dimensions">
          {cfg.width_px && cfg.height_px ? `${cfg.width_px}×${cfg.height_px}px` : DASH}
        </Field>
        <Field label="Fit">{cfg.fit}</Field>
      </dl>
    </div>
  )
}

function CommonFields({ obj }: { obj: PageObjectDetail }) {
  const accent = OBJECT_TYPE_STYLE[obj.object_type].color
  // Geometry (x/y/w/h + z-index) is end-user noise and is hidden here;
  // it stays available read-only via the technical-data button below.
  return (
    <div className="stack stack-8">
      <span className="badge" style={{ background: accent, color: '#fff' }}>{obj.object_type}</span>
      <dl className="detail-grid">
        <Field label="ID">
          <span className="nd-copy-row"><code className="nd-id">{obj.id}</code><CopyButton text={obj.id} what="ID" /></span>
        </Field>
        <Field label="Label">{fmt(obj.label)}</Field>
        <Field label="Description">{fmt(obj.description)}</Field>
        <Field label="Visible">{yesNo(obj.visible)}</Field>
        <Field label="Locked">{yesNo(obj.locked)}</Field>
      </dl>
      <GeometryInfoButton obj={obj} />
    </div>
  )
}

function TextObjectDetailSection({ cfg }: { cfg: TextObjectConfigRead }) {
  return (
    <dl className="detail-grid">
      <Field label="Text">{fmt(cfg.text)}</Field>
      <Field label="Font size">{cfg.font_size}</Field>
      <Field label="Font family">{fmt(cfg.font_family)}</Field>
      <Field label="Style">{textStyleSummary(cfg.bold, cfg.italic)}</Field>
    </dl>
  )
}

function CodeObjectDetailSection({ cfg }: { cfg: CodeObjectConfigRead }) {
  return (
    <dl className="detail-grid">
      <Field label="Code type">{cfg.code_type}</Field>
      <Field label="Encoding">{fmt(cfg.encoding)}</Field>
      <Field label="Value"><code className="nd-id">{fmt(cfg.value)}</code></Field>
      <Field label="Human readable">{yesNo(cfg.human_readable)}</Field>
    </dl>
  )
}

function FreeTextAreaDetailSection({ cfg }: { cfg: FreeTextAreaConfigRead }) {
  return (
    <dl className="detail-grid">
      <Field label="Placeholder">{fmt(cfg.placeholder)}</Field>
      <Field label="Max length">{fmt(cfg.max_length)}</Field>
    </dl>
  )
}

function QuestionObjectDetailSection({ cfg }: { cfg: QuestionObjectConfigRead }) {
  const options = [...cfg.options].sort((a, b) => a.order_index - b.order_index)
  return (
    <div className="stack stack-8">
      <dl className="detail-grid">
        <Field label="Question type">{cfg.question_type}</Field>
        <Field label="Question">{fmt(cfg.question_text)}</Field>
        <Field label="Shuffle">{yesNo(cfg.shuffle_answers)}</Field>
      </dl>
      <div>
        <strong>Options</strong>
        {options.length === 0 ? (
          <p className="nd-muted">{DASH}</p>
        ) : (
          <ul className="nd-options">
            {options.map(o => (
              <li key={o.id} className={o.is_correct ? 'nd-option-correct' : undefined}>
                {o.is_correct ? '● ' : '○ '}{fmt(o.option_text)}
                {o.is_correct && <span className="nd-tag">correct</span>}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}

function MatrixObjectDetailSection({ cfg, objectId }: { cfg: MatrixObjectConfigRead; objectId: string }) {
  const [viewingCells, setViewingCells] = useState(false)
  return (
    <div className="stack stack-8">
      <dl className="detail-grid">
        <Field label="Rows">{cfg.rows_count}</Field>
        <Field label="Cols">{cfg.cols_count}</Field>
        <Field label="Selection rule">{cfg.selection_rule}</Field>
        <Field label="Evaluative">{yesNo(cfg.evaluative)}</Field>
      </dl>
      <div>
        <strong>Row labels</strong>
        <p className="nd-muted">{labelsByOrder(cfg.rows)}</p>
        <strong>Column labels</strong>
        <p className="nd-muted">{labelsByOrder(cfg.cols)}</p>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 4 }}>
          <span className="nd-muted">{cfg.cells.length} cell definition(s)</span>
          <button type="button" className="btn btn-secondary" onClick={() => setViewingCells(true)}>
            View cells
          </button>
        </div>
      </div>
      {viewingCells && (
        // Read-only here: the detail panel only renders for non-DRAFT (read-only) variants.
        <EditCellsModal
          objectId={objectId}
          cfg={cfg}
          readOnly
          onClose={() => setViewingCells(false)}
          onSaved={() => setViewingCells(false)}
        />
      )}
    </div>
  )
}
