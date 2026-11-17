// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'
import type { FormVariantPageRead, PageObjectDetail } from '../api/domain'
import { PageOverlay, type ResizeCorner } from './PageOverlay'
import {
  GRID_STEP,
  MIN_OBJECT_SIZE,
  areaEdgeGuides,
  clampRectToArea,
  clientToPagePoint,
  clipRectToArea,
  normalizeRect,
  resolvePageSize,
  roundRect,
  snapPoint,
  snapRect,
  surfaceMaxWidthPx,
  toPercentRect,
  type PageSize,
  type Rect,
  type SnapGuide,
} from '../utils/viewerCoords'
import type { PreviewMode, ViewerMode } from '../hooks/useVariantWorkspaceState'
import { isAddMode } from '../hooks/useVariantWorkspaceState'
import {
  COLORS, MARKS, holeRect, inReservedBand, markRects, sideMarkRects, usableRect, type MarkCorner,
} from '../utils/exportLayout'

// Page viewer + basic visual editor.
//
// Rendering strategy: a neutral white, A4-proportioned "paper" surface sized
// from the page's width_px/height_px (A4 @ 96 DPI fallback in viewerCoords);
// objects are absolutely positioned via percentage geometry. No page-image API
// is invented. Editing (draw / move / resize) maps pointer coordinates back to
// page pixels via clientToPagePoint and commits through the parent's callbacks.

interface Props {
  page: FormVariantPageRead
  // Enriched objects: each carries its typed config so PageOverlay can
  // render a content preview. Geometry/interaction logic uses only the base fields.
  objects: PageObjectDetail[]
  mode: ViewerMode
  /** STUDENT (blank) vs SOLUTION (correct circles/cells filled) for the preview. */
  previewMode: PreviewMode
  readOnly: boolean
  isLoading: boolean
  isError: boolean
  errorText?: string
  selectedObjectId: string | null
  onSelectObject: (id: string | null) => void
  /** A new box was drawn (only fired in ADD_* mode). */
  onDrawComplete: (rect: Rect) => void
  /** An existing object was moved/resized; commit geometry. */
  onGeometryCommit: (objectId: string, rect: Rect) => void
  /** Snap edits to a 5px grid + render the grid. */
  snapToGrid: boolean
  /** Snap edges/centres to the other objects on the page. */
  snapToObject: boolean
}

type Drag =
  | { kind: 'draw'; start: { x: number; y: number }; last: Rect }
  | { kind: 'move'; id: string; startPage: { x: number; y: number }; startRect: Rect; last: Rect; moved: boolean }
  | { kind: 'resize'; id: string; anchor: { x: number; y: number }; last: Rect }

function rectOf(o: PageObjectDetail): Rect {
  return { x: o.x, y: o.y, width: o.width, height: o.height }
}

function oppositeCorner(r: Rect, corner: ResizeCorner): { x: number; y: number } {
  switch (corner) {
    case 'se': return { x: r.x, y: r.y }
    case 'nw': return { x: r.x + r.width, y: r.y + r.height }
    case 'ne': return { x: r.x, y: r.y + r.height }
    case 'sw': return { x: r.x + r.width, y: r.y }
  }
}

export function VariantPageViewer({
  page, objects, mode, previewMode, readOnly, isLoading, isError, errorText,
  selectedObjectId, onSelectObject, onDrawComplete, onGeometryCommit,
  snapToGrid, snapToObject,
}: Props) {
  const size: PageSize = resolvePageSize(page)
  const editing = mode === 'VIEW' && !readOnly      // objects movable/resizable
  const adding = isAddMode(mode) && !readOnly       // draw new boxes
  // Draw / move / resize stay inside the usable area: the page minus the reserved
  // bands, so every geometry the editor commits is one the server takes.
  const area: Rect = usableRect(size.width, size.height)

  const surfaceRef = useRef<HTMLDivElement | null>(null)
  // Uniform page-px → CSS-px factor for the faithful preview. Measured
  // from the rendered paper width (the surface keeps the page aspect-ratio, so one
  // factor covers both axes). 0 until first measure → previews render geometry only.
  const [pxScale, setPxScale] = useState(0)
  useEffect(() => {
    const el = surfaceRef.current
    if (!el) return
    const measure = () => setPxScale(el.clientWidth > 0 ? el.clientWidth / size.width : 0)
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [size.width])
  const dragRef = useRef<Drag | null>(null)
  const justDraggedRef = useRef(false)
  const [ghost, setGhost] = useState<Rect | null>(null)
  const [preview, setPreview] = useState<{ id: string; rect: Rect } | null>(null)
  // Alignment guides latched during the live drag; cleared on pointer-up.
  const [guides, setGuides] = useState<SnapGuide[]>([])
  // Edges of the usable area the dragged object is stopped on.
  const [bandGuides, setBandGuides] = useState<SnapGuide[]>([])

  const ordered = [...objects].sort((a, b) => a.z_index - b.z_index)

  // Other objects' rects (page px) used as snap targets, excluding `excludeId`.
  function snapTargets(excludeId?: string): Rect[] {
    return objects.filter(o => o.id !== excludeId).map(rectOf)
  }

  function pagePoint(clientX: number, clientY: number): { x: number; y: number } {
    const el = surfaceRef.current
    if (!el) return { x: 0, y: 0 }
    const r = el.getBoundingClientRect()
    return clientToPagePoint(clientX, clientY, { left: r.left, top: r.top, width: r.width, height: r.height }, size)
  }
  function capture(e: ReactPointerEvent) {
    surfaceRef.current?.setPointerCapture?.(e.pointerId)
  }

  function onSurfacePointerDown(e: ReactPointerEvent) {
    if (!adding) return  // VIEW: selection via overlays, clearing via onClick
    const p = pagePoint(e.clientX, e.clientY)
    const last: Rect = { x: p.x, y: p.y, width: 0, height: 0 }
    dragRef.current = { kind: 'draw', start: p, last }
    setGhost(last)
    capture(e)
  }

  function beginMove(obj: PageObjectDetail, e: ReactPointerEvent) {
    const startRect = rectOf(obj)
    dragRef.current = {
      kind: 'move', id: obj.id, startPage: pagePoint(e.clientX, e.clientY),
      startRect, last: startRect, moved: false,
    }
    setPreview({ id: obj.id, rect: startRect })
    capture(e)
  }

  function beginResize(obj: PageObjectDetail, corner: ResizeCorner, e: ReactPointerEvent) {
    const r = rectOf(obj)
    dragRef.current = { kind: 'resize', id: obj.id, anchor: oppositeCorner(r, corner), last: r }
    setPreview({ id: obj.id, rect: r })
    capture(e)
  }

  function onSurfacePointerMove(e: ReactPointerEvent) {
    const d = dragRef.current
    if (!d) return
    const p = pagePoint(e.clientX, e.clientY)
    if (d.kind === 'draw') {
      // Draw: snap the whole box (grid latches both corners + size).
      const snapped = snapRect(normalizeRect(d.start, p), {
        grid: snapToGrid, object: snapToObject, others: snapTargets(), snapSize: true,
      })
      const rect = clipRectToArea(snapped.rect, area)
      d.last = rect
      setGhost(rect)
      setGuides(snapped.guides)
      setBandGuides(areaEdgeGuides(snapped.rect, area))
    } else if (d.kind === 'move') {
      const dx = p.x - d.startPage.x
      const dy = p.y - d.startPage.y
      if (Math.abs(dx) > 2 || Math.abs(dy) > 2) d.moved = true
      // Move: rigid translation — snap position only, keep the object's size.
      const snapped = snapRect({ ...d.startRect, x: d.startRect.x + dx, y: d.startRect.y + dy }, {
        grid: snapToGrid, object: snapToObject, others: snapTargets(d.id), snapSize: false,
      })
      const rect = clampRectToArea(snapped.rect, area)
      d.last = rect
      setPreview({ id: d.id, rect })
      setGuides(snapped.guides)
      setBandGuides(areaEdgeGuides(snapped.rect, area))
    } else {
      // Resize: snap the dragged corner so the anchored corner stays put.
      const snapped = snapPoint(p, { grid: snapToGrid, object: snapToObject, others: snapTargets(d.id) })
      const asked = normalizeRect(d.anchor, snapped.point)
      const rect = clipRectToArea(asked, area)
      d.last = rect
      setPreview({ id: d.id, rect })
      setGuides(snapped.guides)
      setBandGuides(areaEdgeGuides(asked, area))
    }
  }

  // Commits round to integer page px, then re-clamp: rounding both x and width up
  // could push the far edge 1 px into a band (e.g. 600.5 + 136.5 → 601 + 137).
  function onSurfacePointerUp(e: ReactPointerEvent) {
    const d = dragRef.current
    if (!d) return
    dragRef.current = null
    surfaceRef.current?.releasePointerCapture?.(e.pointerId)
    setGuides([])
    setBandGuides([])

    if (d.kind === 'draw') {
      setGhost(null)
      if (d.last.width >= MIN_OBJECT_SIZE && d.last.height >= MIN_OBJECT_SIZE) {
        justDraggedRef.current = true
        onDrawComplete(clampRectToArea(roundRect(d.last), area))
      }
    } else {
      setPreview(null)
      if (d.kind === 'resize' || d.moved) {
        justDraggedRef.current = true
        onGeometryCommit(d.id, clampRectToArea(roundRect(d.last), area))
      }
    }
  }

  // A click that concludes a real drag must not also clear selection.
  function onSurfaceClick() {
    if (justDraggedRef.current) { justDraggedRef.current = false; return }
    if (mode === 'VIEW') onSelectObject(null)
  }

  const surfaceClasses = ['nd-page-surface', adding ? 'is-adding' : ''].filter(Boolean).join(' ')

  return (
    <div className="nd-viewer">
      <ViewerCaption page={page} width={size.width} height={size.height} />

      <div className="nd-viewer-scroll">
        <div
          ref={surfaceRef}
          className={surfaceClasses}
          style={{ aspectRatio: `${size.width} / ${size.height}`, maxWidth: surfaceMaxWidthPx(size.width) }}
          onPointerDown={onSurfacePointerDown}
          onPointerMove={onSurfacePointerMove}
          onPointerUp={onSurfacePointerUp}
          onClick={onSurfaceClick}
        >
          <ReservedBands pageSize={size} />

          {snapToGrid && <GridOverlay pageSize={size} />}

          {ordered.map(obj => {
            const rect = preview && preview.id === obj.id ? preview.rect : rectOf(obj)
            return (
              <PageOverlay
                key={obj.id}
                obj={obj}
                rect={rect}
                pageSize={size}
                inBand={!readOnly && inReservedBand(rect, size.width, size.height)}
                pxScale={pxScale}
                previewMode={previewMode}
                selected={obj.id === selectedObjectId}
                editable={editing}
                onSelect={onSelectObject}
                onBeginMove={(e) => beginMove(obj, e)}
                onBeginResize={(corner, e) => beginResize(obj, corner, e)}
              />
            )
          })}

          <AlignmentMarks pageSize={size} />

          {ghost && <GhostRect rect={ghost} pageSize={size} />}

          {guides.map((g, i) => (
            <GuideLine key={`${g.orient}-${g.pos}-${i}`} guide={g} pageSize={size} />
          ))}
          {bandGuides.map(g => (
            <GuideLine key={`band-${g.orient}-${g.pos}`} guide={g} pageSize={size} band />
          ))}
        </div>
      </div>

      {isLoading && <p className="info-msg">Loading objects…</p>}
      {isError && <p className="error-msg">{errorText ?? 'Failed to load objects.'}</p>}
      {!isLoading && !isError && objects.length === 0 && (
        <p className="info-msg">No objects on this page{editing ? ' — pick a tool above and draw a box.' : '.'}</p>
      )}
    </div>
  )
}

function GhostRect({ rect, pageSize }: { rect: Rect; pageSize: PageSize }) {
  const { leftPct, topPct, widthPct, heightPct } = toPercentRect(rect, pageSize)
  return (
    <div
      className="nd-ghost"
      style={{ left: `${leftPct}%`, top: `${topPct}%`, width: `${widthPct}%`, height: `${heightPct}%` }}
    />
  )
}

/**
 * Light 5px grid drawn behind the objects. Cell size is expressed in
 * percentages of the surface, so the grid stays a true 5 page-px regardless of
 * how the "paper" is scaled, and it never intercepts clicks (pointer-events:none).
 */
function GridOverlay({ pageSize }: { pageSize: PageSize }) {
  const cellW = (GRID_STEP / pageSize.width) * 100
  const cellH = (GRID_STEP / pageSize.height) * 100
  return (
    <div
      className="nd-grid-overlay"
      style={{ backgroundSize: `${cellW}% ${cellH}%` }}
      aria-hidden
    />
  )
}

/**
 * The four alignment marks the export prints on every page, drawn
 * like the PDF: above the objects, solid BLACK, the distinct corner with a WHITE
 * hole; then the side marks, left and right row by row, on the same
 * layer and with the same rules. Percentage geometry from markRects and
 * sideMarkRects, so they scale with the paper; never intercept pointer events and
 * are not objects (not selectable, not editable).
 */
function AlignmentMarks({ pageSize }: { pageSize: PageSize }) {
  const marks = markRects(pageSize.width, pageSize.height)
  const corners = Object.keys(marks) as MarkCorner[]
  return (
    <>
      {corners.map(corner => {
        const mark = marks[corner]
        const p = toPercentRect(mark, pageSize)
        const hole = corner === MARKS.DISTINCT_CORNER ? holeRect(mark) : null
        return (
          <div
            key={corner}
            className="nd-alignment-mark"
            data-corner={corner}
            style={{
              left: `${p.leftPct}%`, top: `${p.topPct}%`, width: `${p.widthPct}%`, height: `${p.heightPct}%`,
              background: COLORS.BLACK,
            }}
            aria-hidden
          >
            {hole && (
              <div
                className="nd-alignment-mark-hole"
                style={{
                  left: `${((hole.x - mark.x) / mark.width) * 100}%`,
                  top: `${((hole.y - mark.y) / mark.height) * 100}%`,
                  width: `${(hole.width / mark.width) * 100}%`,
                  height: `${(hole.height / mark.height) * 100}%`,
                  background: COLORS.WHITE,
                }}
              />
            )}
          </div>
        )
      })}
      {sideMarkRects(pageSize.width, pageSize.height).flatMap((row, i) =>
        (['L', 'R'] as const).map(side => {
          const p = toPercentRect(row[side], pageSize)
          return (
            <div
              key={`side-${side}-${i + 1}`}
              className="nd-alignment-mark"
              data-side={side}
              data-row={i + 1}
              style={{
                left: `${p.leftPct}%`, top: `${p.topPct}%`, width: `${p.widthPct}%`, height: `${p.heightPct}%`,
                background: COLORS.BLACK,
              }}
              aria-hidden
            />
          )
        }),
      )}
    </>
  )
}

/**
 * The four reserved bands of 15 mm along the edges, with a dashed
 * line on the edge of the usable area. Under every object and the marks, never
 * intercept pointer events; shown on every page and in every variant status,
 * because they show what gets printed.
 */
function ReservedBands({ pageSize }: { pageSize: PageSize }) {
  const { width: w, height: h } = pageSize
  const b = MARKS.BAND_W
  const bands: Rect[] = [
    { x: 0, y: 0, width: w, height: b },
    { x: 0, y: h - b, width: w, height: b },
    { x: 0, y: b, width: b, height: h - 2 * b },
    { x: w - b, y: b, width: b, height: h - 2 * b },
  ]
  const pctStyle = (r: Rect) => {
    const p = toPercentRect(r, pageSize)
    return { left: `${p.leftPct}%`, top: `${p.topPct}%`, width: `${p.widthPct}%`, height: `${p.heightPct}%` }
  }
  return (
    <>
      {bands.map((r, i) => (
        <div key={i} className="nd-reserved-band" style={pctStyle(r)} aria-hidden />
      ))}
      <div className="nd-usable-area" style={pctStyle(usableRect(w, h))} aria-hidden />
    </>
  )
}

/**
 * A thin guide line: an edge/centre latched by object-snap, or (`band`) the edge
 * of the usable area the dragged object is stopped on (red).
 */
function GuideLine({ guide, pageSize, band = false }: { guide: SnapGuide; pageSize: PageSize; band?: boolean }) {
  const style =
    guide.orient === 'v'
      ? { left: `${(guide.pos / pageSize.width) * 100}%`, top: 0, bottom: 0, width: 1 }
      : { top: `${(guide.pos / pageSize.height) * 100}%`, left: 0, right: 0, height: 1 }
  return <div className={band ? 'nd-snap-guide nd-band-guide' : 'nd-snap-guide'} style={style} aria-hidden />
}

function ViewerCaption({ page, width, height }: { page: FormVariantPageRead; width: number; height: number }) {
  return (
    <div className="nd-viewer-caption">
      Page {page.page_index} · {width} × {height}px
      {page.dpi ? ` @ ${page.dpi} DPI` : ''}
      {!page.image_path && <span className="nd-muted"> · blank canvas (no page image)</span>}
    </div>
  )
}
