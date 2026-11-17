// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useImperativeHandle, useRef, useState, type KeyboardEvent, type Ref } from 'react'
import type {
  PageObjectDetail,
  ReviewActionType,
  SubmissionReviewObject,
  SubstituteValue,
} from '../api/domain'
import { SubmissionReviewEditor } from './SubmissionReviewEditor'
import {
  formatReviewedAt,
  formatReviewedValue,
  outcomeIcon,
  outcomeLabel,
} from './submissionReview'

const NOTE_MAX_LENGTH = 2000

/**
 * What the page's keyboard listener may do to the action area.
 * Each method is the very handler its button runs, so a key and a click are
 * one code path: same note, same mutation, same next-selection.
 */
export interface ReviewActionsHandle {
  editorOpen: boolean
  noteOpen: boolean
  /** CONFIRM / FLAG / UNDO, exactly as the button. */
  act: (action: 'CONFIRM' | 'FLAG' | 'UNDO') => void
  /** Replace…, exactly as the button. */
  openEditor: () => void
  /** Escape: close the editor, else the note, sending nothing. */
  closeOpen: () => void
}

interface Props {
  obj: SubmissionReviewObject
  /** The authored object, for option letters / matrix labels; undefined while
      it loads or for a type that has none. */
  config: PageObjectDetail | undefined
  /** The lock is ours and the submission is SCANNED: the actions are shown.
      Otherwise only the outcome block, when there is one. */
  canAct: boolean
  /** An action (or "Accept all remaining") is in flight: everything disabled. */
  busy: boolean
  /** A 422 for this reading's last SUBSTITUTE, shown under the open editor. */
  editorError: string | null
  onAction: (action: ReviewActionType, note: string | null, value?: SubstituteValue) => void
  /** Filled with this component's ReviewActionsHandle. */
  handleRef?: Ref<ReviewActionsHandle>
}

/**
 * What an operator can do to the selected reading,
 * under the proposed reading in the focus panel: the current ruling, and —
 * with the lock in hand — Confirm, Replace…, Flag and Undo.
 *
 * Presentational: the page owns the mutation, passes `busy` and receives the
 * action through `onAction`. The local state (the open editor, the note being
 * typed) is reset by the focus remounting this component whenever the
 * selection or the reading's ruling changes — so a successful save closes the
 * editor and a stale note is never sent with the next action.
 *
 * `confirmable` is the server's: Confirm is not rendered without it, and one
 * line says why.
 */
export function SubmissionReviewActions({
  obj, config, canAct, busy, editorError, onAction, handleRef,
}: Props) {
  // Prefilled with the current note, so re-ruling a resolved reading keeps
  // its note unless the operator changes it.
  const [note, setNote] = useState(obj.review_note ?? '')
  const [noteOpen, setNoteOpen] = useState(false)
  const [editing, setEditing] = useState(false)

  // Where focus goes back when the editor or the note closes, so the page's
  // keys work again at once (a removed element would leave it nowhere).
  const rootRef = useRef<HTMLDivElement>(null)

  const noteValue = note.trim() === '' ? null : note.trim()
  const act = (action: ReviewActionType, value?: SubstituteValue) => onAction(action, noteValue, value)

  const closeEditor = () => {
    setEditing(false)
    rootRef.current?.focus()
  }
  // Escape on the note: collapse it and drop what was typed — "close without
  // sending" — back to the current note.
  const closeNote = () => {
    setNote(obj.review_note ?? '')
    setNoteOpen(false)
    rootRef.current?.focus()
  }

  useImperativeHandle(handleRef, () => ({
    editorOpen: editing,
    noteOpen,
    act: action => act(action),
    openEditor: () => setEditing(true),
    closeOpen: () => { if (editing) closeEditor(); else if (noteOpen) closeNote() },
  }))

  const onNoteKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Escape') { e.preventDefault(); closeNote() }
  }

  return (
    <>
      {obj.review_outcome && <OutcomeBlock obj={obj} config={config} />}

      {canAct && (
        <div className="nd-review-actions" ref={rootRef} tabIndex={-1}>
          {editing ? (
            <SubmissionReviewEditor
              obj={obj}
              config={config}
              busy={busy}
              error={editorError}
              onSave={value => act('SUBSTITUTE', value)}
              onCancel={closeEditor}
            />
          ) : obj.resolved ? (
            <div className="nd-review-actions-row">
              <button type="button" className="btn btn-secondary btn-sm" disabled={busy} aria-keyshortcuts="U" onClick={() => act('UNDO')}>
                Undo <Kbd>U</Kbd>
              </button>
              {obj.confirmable && (
                <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={() => act('CONFIRM')}>
                  Confirm
                </button>
              )}
              <button type="button" className="btn btn-secondary btn-sm" disabled={busy} aria-keyshortcuts="R" onClick={() => setEditing(true)}>
                Replace… <Kbd>R</Kbd>
              </button>
              <button type="button" className="btn btn-danger-outline btn-sm" disabled={busy} aria-keyshortcuts="F" onClick={() => act('FLAG')}>
                Flag <Kbd>F</Kbd>
              </button>
            </div>
          ) : (
            <>
              {!obj.confirmable && (
                <p className="nd-replace-hint nd-replace-hint-warn nd-review-actions-warning">
                  There is nothing to confirm: replace it with what the sheet shows, or flag it.
                </p>
              )}
              <div className="nd-review-actions-row">
                {obj.confirmable && (
                  <button type="button" className="btn btn-primary btn-sm" disabled={busy} aria-keyshortcuts="Enter" onClick={() => act('CONFIRM')}>
                    Confirm <Kbd>Enter</Kbd>
                  </button>
                )}
                <button
                  type="button"
                  className={`btn ${obj.confirmable ? 'btn-secondary' : 'btn-primary'} btn-sm`}
                  disabled={busy}
                  aria-keyshortcuts="R"
                  onClick={() => setEditing(true)}
                >
                  Replace… <Kbd>R</Kbd>
                </button>
                <button type="button" className="btn btn-danger-outline btn-sm" disabled={busy} aria-keyshortcuts="F" onClick={() => act('FLAG')}>
                  Flag unresolvable <Kbd>F</Kbd>
                </button>
              </div>
            </>
          )}

          {noteOpen ? (
            <textarea
              className="nd-review-input nd-review-note"
              aria-label="Note"
              rows={2}
              placeholder="Note (optional)"
              value={note}
              maxLength={NOTE_MAX_LENGTH}
              disabled={busy}
              autoFocus
              onChange={e => setNote(e.target.value)}
              onKeyDown={onNoteKeyDown}
            />
          ) : (
            <button type="button" className="nd-link-btn nd-review-note-toggle" onClick={() => setNoteOpen(true)}>
              {note.trim() === '' ? '+ Add note' : 'Edit note'}
            </button>
          )}

          <p className="nd-keys-row" aria-hidden="true">
            <Kbd>Enter</Kbd> confirm &amp; next · <Kbd>R</Kbd> replace · <Kbd>F</Kbd> flag ·{' '}
            <Kbd>U</Kbd> undo · <Kbd>↑</Kbd> / <Kbd>↓</Kbd> previous / next
          </p>
        </div>
      )}
    </>
  )
}

/** A key hint inside a button or the keys row. Hidden from assistive tech:
    the buttons carry aria-keyshortcuts instead. Hidden under 640px (CSS). */
export function Kbd({ children }: { children: string }) {
  return <kbd className="nd-kbd" aria-hidden="true">{children}</kbd>
}

/** The current ruling: what, who, when, and the note. Shown in
    every view, read-only ones included. */
function OutcomeBlock({ obj, config }: { obj: SubmissionReviewObject; config: PageObjectDetail | undefined }) {
  const outcome = obj.review_outcome!
  const label = outcome === 'SUBSTITUTED' ? formatReviewedValue(obj, config) : outcomeLabel(outcome)
  const when = obj.reviewed_at ? formatReviewedAt(obj.reviewed_at) : ''
  const who = [obj.reviewed_by?.display_name, when].filter(Boolean).join(' · ')
  return (
    <div className={`nd-review-outcome nd-review-outcome-${outcome.toLowerCase()}`} role="status">
      <b>
        <span aria-hidden="true">{outcomeIcon(outcome)} </span>
        {label}
      </b>
      {who && <span className="nd-review-outcome-who"> · {who}</span>}
      {obj.review_note && <div className="nd-review-outcome-note">“{obj.review_note}”</div>}
    </div>
  )
}
