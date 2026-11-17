// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import type { TextDraft } from './objectDraft'

/** Controlled TEXT config editor: edits the draft, no own Save. */
export function TextObjectEditor({ value, onChange }: { value: TextDraft; onChange: (patch: Partial<TextDraft>) => void }) {
  return (
    <div className="stack stack-8">
      <div className="form-group"><label>Text</label><textarea rows={2} value={value.text} placeholder="Static text to print on the page" onChange={e => onChange({ text: e.target.value })} /></div>
      <div className="nd-grid-2">
        <div className="form-group"><label>Font size</label><input type="number" min={1} value={value.font_size} onChange={e => onChange({ font_size: parseInt(e.target.value, 10) || 1 })} /></div>
        <div className="form-group"><label>Font family</label><input value={value.font_family} onChange={e => onChange({ font_family: e.target.value })} /></div>
      </div>
      <div className="row row-12">
        <label className="nd-check"><input type="checkbox" checked={value.bold} onChange={e => onChange({ bold: e.target.checked })} /> Bold</label>
        <label className="nd-check"><input type="checkbox" checked={value.italic} onChange={e => onChange({ italic: e.target.checked })} /> Italic</label>
      </div>
    </div>
  )
}
