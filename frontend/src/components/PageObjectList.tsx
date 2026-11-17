// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import type { PageObjectListItem } from '../api/domain'
import { OBJECT_TYPE_STYLE, objectDisplayLabel } from '../utils/objectDisplay'
import { ROLE_LABELS } from '../utils/pageObjectRole'
import { inReservedBand } from '../utils/exportLayout'
import type { PageSize } from '../utils/viewerCoords'
import { RESERVED_BAND_HINT } from './PageOverlay'

interface Props {
  objects: PageObjectListItem[]
  isLoading: boolean
  isError: boolean
  errorText?: string
  selectedObjectId: string | null
  onSelect: (id: string) => void
  /** Page size to flag the objects inside a reserved band; null when
   *  nothing is flagged (variant not a draft). */
  bandCheckPage: PageSize | null
}

/**
 * Controlled object list for the selected page. Selection lives in the parent
 * so the list and the page overlay stay in sync. Read-only.
 */
export function PageObjectList({
  objects, isLoading, isError, errorText, selectedObjectId, onSelect, bandCheckPage,
}: Props) {
  // Front-to-back: the top-most object (highest z_index) is listed first, the
  // background last — the reverse of the viewer's back-to-front stacking. Ties
  // break on created_at (later = drawn on top), mirroring the BE ordering.
  const ordered = [...objects].sort(
    (a, b) => b.z_index - a.z_index || b.created_at.localeCompare(a.created_at),
  )
  return (
    <div className="nd-panel-section">
      <h3 className="mb-16">Objects {objects.length > 0 && <span className="nd-muted">({objects.length})</span>}</h3>

      {isLoading && <p className="info-msg">Loading…</p>}
      {isError && <p className="error-msg">{errorText ?? 'Failed to load objects.'}</p>}
      {!isLoading && !isError && objects.length === 0 && (
        <p className="info-msg">No objects on this page yet.</p>
      )}

      {objects.length > 0 && (
        <ul className="nd-object-list">
          {ordered.map(o => (
            <li
              key={o.id}
              className={`nd-object-item ${o.id === selectedObjectId ? 'is-selected' : ''}`}
              onClick={() => onSelect(o.id)}
            >
              <span className="nd-object-dot" style={{ background: OBJECT_TYPE_STYLE[o.object_type].color }} />
              <span className="nd-object-name">{objectDisplayLabel(o)}</span>
              {bandCheckPage && inReservedBand(o, bandCheckPage.width, bandCheckPage.height) && (
                <span className="nd-object-band" title={RESERVED_BAND_HINT}>!</span>
              )}
              <span className="nd-object-type nd-muted">{o.object_type}</span>
              {!o.visible && <span className="nd-tag">hidden</span>}
              {o.locked && <span className="nd-tag">🔒</span>}
              {o.shared_across_pages && <span className="nd-tag" title="Shown on all pages">🔗</span>}
              {o.role && <span className="nd-tag" title={ROLE_LABELS[o.role]}>🏷</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
