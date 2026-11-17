// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import type { GeometryBase } from '../../api/domain'

export interface GeometryState {
  x: number; y: number; width: number; height: number; z_index: number; label: string
  /** "Show on all pages" — create the object as a linked template. */
  sharedAcrossPages: boolean
}

export const DEFAULT_GEOMETRY: GeometryState = {
  x: 50, y: 50, width: 200, height: 100, z_index: 0, label: '', sharedAcrossPages: false,
}

/** Turn the editable geometry state into the backend GeometryBase payload. */
export function geometryToPayload(g: GeometryState): GeometryBase {
  return {
    x: g.x, y: g.y, width: g.width, height: g.height,
    z_index: g.z_index,
    label: g.label.trim() || null,
    shared_across_pages: g.sharedAcrossPages,
  }
}

interface Props {
  value: GeometryState
  onChange: (next: GeometryState) => void
}

/**
 * Presentation fields for every object form. Geometry (x/y/width/height/z-index)
 * is intentionally NOT shown: position and size come from the box the
 * user drew on the canvas and ride along in `value`, so only the label is edited
 * here. The geometry values still flow through to `geometryToPayload` on submit.
 */
export function GeometryFields({ value, onChange }: Props) {
  return (
    <div className="stack stack-8">
      <div className="form-group">
        <label>label</label>
        <input value={value.label} onChange={e => onChange({ ...value, label: e.target.value })} />
      </div>
      <label className="nd-check" title="The object is added to new pages automatically. Existing pages are left as-is; you can unlink it later from the editor.">
        <input
          type="checkbox"
          checked={value.sharedAcrossPages}
          onChange={e => onChange({ ...value, sharedAcrossPages: e.target.checked })}
        />{' '}
        🔗 Show on all pages
      </label>
    </div>
  )
}
