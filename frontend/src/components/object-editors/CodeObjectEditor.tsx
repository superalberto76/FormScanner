// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { CODE_ENCODINGS, type CodeEncoding, type CodeType } from '../../api/domain'
import type { CodeDraft } from './objectDraft'

/**
 * Controlled CODE config editor: edits the draft, no own Save.
 *
 * `eligibleForPageIndex` is non-draft context computed by
 * ObjectEditPanel from the object's role / shared siblings — it only gates the
 * "Include page number" checkbox; the value itself rides the normal draft/Save.
 */
export function CodeObjectEditor({ value, onChange, eligibleForPageIndex }: {
  value: CodeDraft; onChange: (patch: Partial<CodeDraft>) => void; eligibleForPageIndex: boolean
}) {
  // BARCODE requires an encoding (default CODE128); QR must have none.
  function changeCodeType(next: CodeType) {
    onChange({ code_type: next, encoding: next === 'BARCODE' ? 'CODE128' : null })
  }

  return (
    <div className="stack stack-8">
      <div className="form-group">
        <label>Code type</label>
        <select value={value.code_type} onChange={e => changeCodeType(e.target.value as CodeType)}>
          <option value="QR">QR</option>
          <option value="BARCODE">BARCODE</option>
        </select>
      </div>
      <div className="form-group"><label>Value</label><input value={value.value} placeholder="Encoded value" onChange={e => onChange({ value: e.target.value })} /></div>
      {value.code_type === 'BARCODE' && (
        <div className="form-group">
          <label>Encoding</label>
          <select value={value.encoding ?? 'CODE128'} onChange={e => onChange({ encoding: e.target.value as CodeEncoding })}>
            {CODE_ENCODINGS.map(enc => <option key={enc} value={enc}>{enc}</option>)}
          </select>
        </div>
      )}
      <label className="nd-check"><input type="checkbox" checked={value.human_readable} onChange={e => onChange({ human_readable: e.target.checked })} /> Human readable</label>
      <label className="nd-check">
        <input
          type="checkbox"
          checked={value.include_page_index}
          disabled={!eligibleForPageIndex}
          onChange={e => onChange({ include_page_index: e.target.checked })}
        />
        Include page number
      </label>
      {!eligibleForPageIndex && (
        <p className="nd-muted nd-edit-hint">
          Requires the 'Variant identifier' role on this object or on one of its
          shared copies.
        </p>
      )}
    </div>
  )
}
