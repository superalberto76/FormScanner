// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { Fragment, memo, useRef, type PointerEvent as ReactPointerEvent, type ReactNode } from 'react'
import type {
  CodeObjectConfigRead,
  FreeTextAreaConfigRead,
  MatrixObjectConfigRead,
  PageObjectDetail,
  QuestionObjectConfigRead,
  TextObjectConfigRead,
} from '../api/domain'
import { useObjectImage } from '../hooks/useObjectImage'
import { useAutofitFontSize } from '../hooks/useAutofitFontSize'
import type { PreviewMode } from '../hooks/useVariantWorkspaceState'
import { pickPreviewKind } from '../utils/objectPreview'
import {
  isCircleFilled,
  matrixLabel,
  visibleQuestionOptionCount,
} from '../utils/objectPreviewLayout'
import {
  AUTOFIT,
  CODE,
  COLORS,
  cssFontStack,
  FT,
  IMAGE_PLACEHOLDER,
  M,
  Q,
} from '../utils/exportLayout'
import { OBJECT_TYPE_STYLE, objectDisplayLabel } from '../utils/objectDisplay'
import { toPercentRect, type PageSize, type Rect } from '../utils/viewerCoords'

export type ResizeCorner = 'nw' | 'ne' | 'sw' | 'se'
/** Tooltip of an object inside a reserved band, canvas and object list. */
export const RESERVED_BAND_HINT = 'Inside the reserved band: move it inside the page area.'
const CORNERS: ResizeCorner[] = ['nw', 'ne', 'sw', 'se']

interface Props {
  obj: PageObjectDetail
  /** Geometry to render (the object's own, or a live drag/resize preview). */
  rect: Rect
  pageSize: PageSize
  /** The rect crosses a reserved band; the viewer passes it only on a
   *  draft, where the author can still move the object. */
  inBand: boolean
  /** Page-px → CSS-px factor for the faithful preview (uniform surface scale). */
  pxScale: number
  /** STUDENT (blank) vs SOLUTION (correct circles/cells filled). */
  previewMode: PreviewMode
  selected: boolean
  /** When true (DRAFT + VIEW mode), the body is draggable and handles show. */
  editable: boolean
  onSelect: (id: string) => void
  onBeginMove: (e: ReactPointerEvent) => void
  onBeginResize: (corner: ResizeCorner, e: ReactPointerEvent) => void
}

/**
 * One absolutely-positioned object overlay. Read-only by default; when
 * `editable`, pointer-down on the body starts a move and corner handles start a
 * resize (the parent viewer owns the actual pointer math).
 *
 * A `locked` object stays selectable/inspectable but is never movable or
 * resizable: no drag is armed on press and no resize handles are rendered.
 *
 * The first child is a FAITHFUL content preview whose layout mirrors
 * the PDF export — drawn as a pointer-transparent layer beneath the chip / icons
 * / handles. It overlays the geometry without altering it, so draw / move /
 * resize / z-order are unaffected.
 *
 * Wrapped in `memo`: during a drag the parent re-renders on every pointermove
 * (preview-rect state churn), but the props here are stable for non-dragged
 * overlays, so React skips re-rendering them (and their preview subtree).
 */
export const PageOverlay = memo(function PageOverlay({
  obj, rect, pageSize, inBand, pxScale, previewMode, selected, editable, onSelect, onBeginMove, onBeginResize,
}: Props) {
  const style = OBJECT_TYPE_STYLE[obj.object_type]
  const { leftPct, topPct, widthPct, heightPct } = toPercentRect(rect, pageSize)
  // Mode says "editable", but a locked object opts out of move/resize.
  const movable = editable && !obj.locked

  function onBodyPointerDown(e: ReactPointerEvent) {
    if (!editable) return
    e.stopPropagation()
    onSelect(obj.id)        // select on press so a press-without-drag still selects
    if (movable) onBeginMove(e)  // locked: select but never start a move
  }

  return (
    <div
      role="button"
      tabIndex={0}
      aria-pressed={selected}
      title={`${obj.object_type} · ${objectDisplayLabel(obj)}${obj.locked ? ' · 🔒 locked' : ''}${obj.shared_across_pages ? ' · 🔗 shown on all pages' : ''}${inBand ? `\n${RESERVED_BAND_HINT}` : ''}`}
      className={`nd-overlay ${selected ? 'is-selected' : ''} ${movable ? 'is-editable' : ''} ${editable && obj.locked ? 'is-locked' : ''} ${inBand ? 'is-in-band' : ''}`}
      style={{
        left: `${leftPct}%`,
        top: `${topPct}%`,
        width: `${widthPct}%`,
        height: `${heightPct}%`,
        borderColor: style.color,
        background: style.fill,
        // Selected overlay floats above its siblings; this is local to the
        // paper's stacking context (.nd-page-surface { isolation }), so it never
        // escapes above a modal backdrop.
        zIndex: selected ? 'var(--z-overlay-selected)' : obj.z_index,
        opacity: obj.visible ? 1 : 0.45,
      }}
      onPointerDown={onBodyPointerDown}
      onClick={(e) => { e.stopPropagation(); if (!editable) onSelect(obj.id) }}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect(obj.id) } }}
    >
      <ObjectPreview obj={obj} pxScale={pxScale} previewMode={previewMode} />
      <span className="nd-overlay-chip" style={{ background: style.color }}>
        {objectDisplayLabel(obj)}
      </span>
      {editable && obj.locked && <span className="nd-overlay-lock" aria-hidden="true">🔒</span>}
      {obj.shared_across_pages && <span className="nd-overlay-shared" aria-hidden="true">🔗</span>}
      {inBand && <span className="nd-overlay-band-warning" aria-hidden="true">!</span>}
      {movable && selected && CORNERS.map(c => (
        <span
          key={c}
          className={`nd-handle nd-handle-${c}`}
          style={{ borderColor: style.color }}
          onPointerDown={(e) => { e.stopPropagation(); onBeginResize(c, e) }}
        />
      ))}
    </div>
  )
})

// ── Faithful content preview ──────────────────────────────────────────────────
// A WYSIWYG-ish layer whose geometry matches the PDF export (fidelity, not pixel
// parity). Every coordinate below is `<export constant> * pxScale`, the constants
// coming from exportLayout.ts (the canvas twin of the Python export modules). The
// layer is pointer-transparent and clips its own overflow, so it never touches
// the overlay's draw/move/resize/z-order behaviour.

interface PreviewBase {
  obj: PageObjectDetail
  pxScale: number
}
interface PreviewProps extends PreviewBase {
  previewMode: PreviewMode
}

/**
 * Faithful preview for an object, drawn as a background layer inside its overlay.
 * Returns null for hidden objects (geometry only) and during the pre-measure
 * frame (`pxScale <= 0`). Memoized on its props (`obj` identity is stable across a
 * drag), so this heavier subtree is skipped while the parent churns the live
 * drag rect, and recomputes only when the object itself is resized/edited.
 */
const ObjectPreview = memo(function ObjectPreview({ obj, pxScale, previewMode }: PreviewProps) {
  if (pxScale <= 0) return null // pre-measure: render geometry only for one frame
  switch (pickPreviewKind(obj)) { // returns null when !obj.visible → geometry only
    case 'TEXT':           return <TextPreview cfg={obj.text_config} pxScale={pxScale} />
    case 'CODE':           return <CodePreview cfg={obj.code_config} obj={obj} pxScale={pxScale} />
    case 'QUESTION':       return <QuestionPreview cfg={obj.question_config} obj={obj} pxScale={pxScale} previewMode={previewMode} />
    case 'MATRIX':         return <MatrixPreview cfg={obj.matrix_config} obj={obj} pxScale={pxScale} previewMode={previewMode} />
    case 'FREE_TEXT_AREA': return <FreeTextPreview cfg={obj.free_text_config} obj={obj} pxScale={pxScale} />
    case 'IMAGE':          return <ImagePreview obj={obj} pxScale={pxScale} />
    default:               return null
  }
})

/** Multiply a page-px length by the surface scale → CSS px. */
const scaler = (pxScale: number) => (v: number) => v * pxScale

/**
 * TEXT — one autofit text box mirroring `PageRenderer.text`: family via
 * `cssFontStack`, bold/italic, BLACK, `cfg.font_size` start size, WRAP + clip,
 * NO ellipsis. The autofit hook shrinks the size until it fits (or hits the floor).
 */
function TextPreview({ cfg, pxScale }: { cfg?: TextObjectConfigRead; pxScale: number }) {
  const ref = useRef<HTMLDivElement>(null)
  useAutofitFontSize(ref, {
    startPx: cfg?.font_size ?? 0,
    floorPx: AUTOFIT.FLOOR_PX,
    pxScale,
    deps: [cfg?.text, cfg?.font_size, cfg?.bold, cfg?.italic, cfg?.font_family],
  })
  if (!cfg) return null
  return (
    <div
      ref={ref}
      className="nd-preview nd-preview-text"
      aria-hidden="true"
      style={{
        fontFamily: cssFontStack(cfg.font_family),
        fontWeight: cfg.bold ? 700 : 400,
        fontStyle: cfg.italic ? 'italic' : 'normal',
        color: COLORS.BLACK,
      }}
    >
      {cfg.text}
    </div>
  )
}

/**
 * QUESTION — header (size 12 bold) in HEADER_H, then options sorted by
 * order_index, each OPTION_H tall with a circle at CIRCLE_CX / r CIRCLE_R and text
 * at TEXT_X (size 11). Options past the object's height are clipped (the export's
 * break). The circle is filled iff SOLUTION && opt.is_correct.
 */
function QuestionPreview(
  { cfg, obj, pxScale, previewMode }: { cfg?: QuestionObjectConfigRead } & PreviewProps,
) {
  if (!cfg) return null
  const px = scaler(pxScale)
  const options = [...cfg.options].sort((a, b) => a.order_index - b.order_index)
  const shown = options.slice(
    0, visibleQuestionOptionCount(obj.height, options.length, Q.HEADER_H, Q.OPTION_H),
  )
  const diameter = Q.CIRCLE_R * 2
  return (
    <div className="nd-preview" aria-hidden="true">
      <div
        className="nd-preview-line"
        style={{
          left: 0, top: 0, width: px(obj.width), height: px(Q.HEADER_H),
          fontSize: px(Q.HEADER_SIZE), fontWeight: 700, color: COLORS.BLACK,
        }}
      >
        {cfg.question_text}
      </div>
      {shown.map((o, i) => {
        const rowTop = Q.HEADER_H + i * Q.OPTION_H
        const cy = rowTop + Q.OPTION_H / 2
        const filled = isCircleFilled(previewMode, o.is_correct)
        return (
          <Fragment key={o.id}>
            <span
              className="nd-preview-circle"
              style={{
                left: px(Q.CIRCLE_CX - Q.CIRCLE_R), top: px(cy - Q.CIRCLE_R),
                width: px(diameter), height: px(diameter),
                borderColor: COLORS.BLACK, background: filled ? COLORS.BLACK : 'transparent',
              }}
            />
            <div
              className="nd-preview-line"
              style={{
                left: px(Q.TEXT_X), top: px(rowTop),
                width: px(Math.max(obj.width - Q.TEXT_X, 1)), height: px(Q.OPTION_H),
                fontSize: px(Q.OPTION_SIZE), color: COLORS.BLACK,
              }}
            >
              {o.option_text}
            </div>
          </Fragment>
        )
      })}
    </div>
  )
}

/**
 * MATRIX — optional label (size 12 bold) in LABEL_H, then a header strip and a
 * rows×cols grid (LIGHT cell borders) filling the rest. label_w = min(w*0.25, 120).
 * Column headers (size 9, centered) + row labels (size 9); a circle per cell,
 * filled iff SOLUTION && correct. Missing labels default to the index. Not capped.
 */
function MatrixPreview(
  { cfg, obj, pxScale, previewMode }: { cfg?: MatrixObjectConfigRead } & PreviewProps,
) {
  if (!cfg) return null
  const px = scaler(pxScale)
  const { rows_count: rows, cols_count: cols } = cfg
  const rowLabels = new Map(cfg.rows.map(r => [r.order_index, r.label]))
  const colLabels = new Map(cfg.cols.map(c => [c.order_index, c.label]))
  const correct = new Set(
    cfg.cells.filter(c => c.is_correct).map(c => `${c.row_index}:${c.col_index}`),
  )

  // The export always reserves the label strip (top = obj.y + _M_LABEL_H), even
  // with no label text — so the grid sits below LABEL_H + HEADER_H regardless.
  const labelW = Math.min(obj.width * M.LABEL_W_RATIO, M.LABEL_W_MAX)
  const gridX = labelW
  const gridY = M.LABEL_H + M.HEADER_H

  const label = obj.label ? (
    <div
      className="nd-preview-line"
      style={{
        left: 0, top: 0, width: px(obj.width), height: px(M.LABEL_H),
        fontSize: px(M.LABEL_SIZE), fontWeight: 700, color: COLORS.BLACK,
      }}
    >
      {obj.label}
    </div>
  ) : null

  if (rows <= 0 || cols <= 0) {
    return <div className="nd-preview" aria-hidden="true">{label}</div>
  }

  const gridW = Math.max(obj.width - labelW, 1)
  const gridH = Math.max(obj.height - gridY, 1)
  const cellW = gridW / cols
  const cellH = gridH / rows
  const diameter = M.CIRCLE_R * 2

  const headers = Array.from({ length: cols }, (_, c) => (
    <div
      key={`h${c}`}
      className="nd-preview-line nd-preview-center"
      style={{
        left: px(gridX + c * cellW), top: px(M.LABEL_H), width: px(cellW), height: px(M.HEADER_H),
        fontSize: px(M.HEADER_SIZE), color: COLORS.BLACK,
      }}
    >
      {matrixLabel(colLabels, c)}
    </div>
  ))

  const grid: ReactNode[] = []
  for (let rr = 0; rr < rows; rr++) {
    const rowTop = gridY + rr * cellH
    grid.push(
      <div
        key={`r${rr}`}
        className="nd-preview-line"
        style={{
          left: 0, top: px(rowTop), width: px(labelW), height: px(cellH),
          fontSize: px(M.ROW_LABEL_SIZE), color: COLORS.BLACK,
        }}
      >
        {matrixLabel(rowLabels, rr)}
      </div>,
    )
    for (let c = 0; c < cols; c++) {
      const cellLeft = gridX + c * cellW
      const filled = isCircleFilled(previewMode, correct.has(`${rr}:${c}`))
      grid.push(
        <span
          key={`b${rr}:${c}`}
          className="nd-preview-cell"
          style={{
            left: px(cellLeft), top: px(rowTop), width: px(cellW), height: px(cellH),
            borderColor: COLORS.LIGHT,
          }}
        />,
      )
      grid.push(
        <span
          key={`c${rr}:${c}`}
          className="nd-preview-circle"
          style={{
            left: px(cellLeft + cellW / 2 - M.CIRCLE_R), top: px(rowTop + cellH / 2 - M.CIRCLE_R),
            width: px(diameter), height: px(diameter),
            borderColor: COLORS.BLACK, background: filled ? COLORS.BLACK : 'transparent',
          }}
        />,
      )
    }
  }

  return (
    <div className="nd-preview" aria-hidden="true">
      {label}
      {headers}
      {grid}
    </div>
  )
}

/**
 * FREE_TEXT_AREA — optional label (size 10 bold) in LABEL_H, a BLACK box below it,
 * and the placeholder (size 9 italic, LIGHT) padded PAD_X/PAD_Y. If the remaining
 * box height is <= 0, only the label is drawn (mirrors the export).
 */
function FreeTextPreview({ cfg, obj, pxScale }: { cfg?: FreeTextAreaConfigRead } & PreviewBase) {
  const px = scaler(pxScale)
  const hasLabel = !!obj.label
  const boxY = hasLabel ? FT.LABEL_H : 0
  const boxH = obj.height - boxY
  const placeholder = cfg?.placeholder
  return (
    <div className="nd-preview" aria-hidden="true">
      {hasLabel && (
        <div
          className="nd-preview-line"
          style={{
            left: 0, top: 0, width: px(obj.width), height: px(FT.LABEL_H),
            fontSize: px(FT.LABEL_SIZE), fontWeight: 700, color: COLORS.BLACK,
          }}
        >
          {obj.label}
        </div>
      )}
      {boxH > 0 && (
        <span
          className="nd-preview-box"
          style={{ left: 0, top: px(boxY), width: px(obj.width), height: px(boxH), borderColor: COLORS.BLACK }}
        />
      )}
      {boxH > 0 && placeholder && (
        <div
          className="nd-preview-line"
          style={{
            left: px(FT.PAD_X), top: px(boxY + FT.PAD_Y),
            width: px(Math.max(obj.width - 2 * FT.PAD_X, 1)),
            height: px(Math.min(boxH, FT.PLACEHOLDER_MAX_H)),
            fontSize: px(FT.PLACEHOLDER_SIZE), fontStyle: 'italic', color: COLORS.LIGHT,
          }}
        >
          {placeholder}
        </div>
      )}
    </div>
  )
}

/**
 * CODE — always the placeholder (the canvas has no encoder, non-scannable by
 * design): a GRAY box of height `code_h = height - (human_readable ? 14 : 0)` with
 * `[{code_type}]` + value (size 8 GRAY); if human_readable, the value (size 8
 * BLACK, centered) in the bottom 14px strip.
 */
function CodePreview({ cfg, obj, pxScale }: { cfg?: CodeObjectConfigRead } & PreviewBase) {
  if (!cfg) return null
  const px = scaler(pxScale)
  const hr = cfg.human_readable
  const codeH = Math.max(obj.height - (hr ? CODE.HUMAN_READABLE_STRIP_PX : 0), 1)
  return (
    <div className="nd-preview" aria-hidden="true">
      <span
        className="nd-preview-codebox"
        style={{ left: 0, top: 0, width: px(obj.width), height: px(codeH), borderColor: COLORS.GRAY }}
      />
      <div
        className="nd-preview-line nd-preview-pre"
        style={{
          left: px(CODE.PAD), top: px(CODE.PAD),
          width: px(Math.max(obj.width - 2 * CODE.PAD, 1)),
          height: px(Math.max(codeH - 2 * CODE.PAD, 1)),
          fontSize: px(CODE.TEXT_SIZE), color: COLORS.GRAY,
        }}
      >
        {`[${cfg.code_type}]\n${cfg.value}`}
      </div>
      {hr && (
        <div
          className="nd-preview-line nd-preview-center"
          style={{
            left: 0, top: px(codeH), width: px(obj.width), height: px(CODE.HUMAN_READABLE_STRIP_PX),
            fontSize: px(CODE.TEXT_SIZE), color: COLORS.BLACK,
          }}
        >
          {cfg.value}
        </div>
      )}
    </div>
  )
}

/**
 * IMAGE — the uploaded picture with `object-fit: {cfg.fit}` (contain/cover) when a
 * file exists, else a mountain+sun placeholder (label size 9 GRAY). Fetched as an
 * authenticated blob and shown via <img> (a served SVG can't run scripts). Renders
 * nothing while the blob loads — the geometry box is already visible.
 */
function ImagePreview({ obj, pxScale }: { obj: PageObjectDetail; pxScale: number }) {
  const cfg = obj.image_config
  const hasImage = !!cfg?.has_image
  const { url } = useObjectImage(obj.id, hasImage)
  if (!hasImage) {
    return (
      <div className="nd-preview nd-preview-imgph" aria-hidden="true" style={{ color: COLORS.GRAY }}>
        <ImagePlaceholderGlyph />
        <span
          className="nd-preview-imgph-label"
          style={{ fontSize: `${IMAGE_PLACEHOLDER.LABEL_SIZE * pxScale}px` }}
        >
          {obj.label || 'image'}
        </span>
      </div>
    )
  }
  if (!url) return null
  return (
    <div className="nd-preview" aria-hidden="true">
      <img className="nd-preview-img" src={url} alt="" style={{ objectFit: cfg?.fit ?? 'contain' }} />
    </div>
  )
}

// ── Inline glyph (no asset/dependency; aria-hidden, pointer-transparent) ───────

function ImagePlaceholderGlyph() {
  return (
    <svg className="nd-preview-glyph" viewBox="0 0 24 24" aria-hidden="true">
      <rect x="2" y="4" width="20" height="16" rx="2" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <circle cx="8" cy="9" r="1.8" fill="currentColor" />
      <path d="M4 18l5-5 3.5 3.5L16 12l4 4.5V18H4z" fill="currentColor" />
    </svg>
  )
}
