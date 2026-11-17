// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import type { FreeTextDraft } from './objectDraft'

/** Controlled FREE_TEXT_AREA config editor: edits the draft only. */
export function FreeTextAreaEditor({ value, onChange }: { value: FreeTextDraft; onChange: (patch: Partial<FreeTextDraft>) => void }) {
  return (
    <div className="stack stack-8">
      <div className="form-group"><label>Placeholder</label><input value={value.placeholder} placeholder="Hint shown inside the area" onChange={e => onChange({ placeholder: e.target.value })} /></div>
      <div className="form-group"><label>Max length <span className="nd-muted">(blank = no limit)</span></label><input type="number" min={1} value={value.max_length} placeholder="No limit" onChange={e => onChange({ max_length: e.target.value })} /></div>
    </div>
  )
}
