// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import type { PresentationDraft } from './objectDraft'

/**
 * Presentation editor (label / description / visible / locked) shared by every
 * object type. Geometry (x/y/width/height/z-index) is intentionally NOT edited
 * here: position/size come from canvas drag/resize, z-order from the
 * toolbar, and the raw values are read-only in "Technical data".
 *
 * Fully controlled by the object draft: edits land in local state
 * and are persisted only by the panel's single Save. The "Locked" flag therefore
 * does not toggle immediately — it commits with everything else on Save.
 */
export function CommonObjectEditor({ value, onChange }: { value: PresentationDraft; onChange: (patch: Partial<PresentationDraft>) => void }) {
  return (
    <div className="stack stack-8">
      <div className="form-group"><label>Label</label><input value={value.label} onChange={e => onChange({ label: e.target.value })} /></div>
      <div className="form-group"><label>Description</label><input value={value.description} onChange={e => onChange({ description: e.target.value })} /></div>
      <div className="row row-12">
        <label className="nd-check"><input type="checkbox" checked={value.visible} onChange={e => onChange({ visible: e.target.checked })} /> Visible</label>
        <label className="nd-check"><input type="checkbox" checked={value.locked} onChange={e => onChange({ locked: e.target.checked })} /> Locked</label>
      </div>
    </div>
  )
}
