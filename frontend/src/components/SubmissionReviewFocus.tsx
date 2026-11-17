// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import type { Ref } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  nd,
  type ReviewActionType,
  type SubmissionReviewObject,
  type SubmissionReviewPage,
  type SubstituteValue,
} from '../api/domain'
import { SubmissionReviewActions, type ReviewActionsHandle } from './SubmissionReviewActions'
import { ReasonChips } from './SubmissionReviewList'
import { SubmissionScanCrop } from './SubmissionScanCrop'
import { candidateNote, formatProposedReading, objectTitle, reasonExplanation } from './submissionReview'

interface Props {
  obj: SubmissionReviewObject
  /** The sheet the reading is on, for "· Page n" and the scan crop (its id
      and template page come from the review payload: no extra request). */
  page: SubmissionReviewPage
  /** The sheet whose identity reading sets the candidate, for the note
      on a later copy; null when the submission has none. */
  candidatePage: SubmissionReviewPage | null
  /** Whether that reading is in the review list (some listed object has
      `sets_candidate`); when not, the note points to the header. */
  candidateListed: boolean
  /** The batch's effective threshold (a fraction), for the LOW_CONFIDENCE text. */
  threshold: number
  /** Whether the actions are offered (lock ours, SCANNED), whether
      one is in flight, a 422 for this reading's replacement, and the sink. */
  canAct: boolean
  busy: boolean
  editorError: string | null
  onAction: (action: ReviewActionType, note: string | null, value?: SubstituteValue) => void
  /** The action area's handle, for the page's keyboard listener. */
  actionsRef?: Ref<ReviewActionsHandle>
}

/**
 * The right column of the submission review page: the selected reading, its
 * reasons, the scan around it and what the extractor proposes, as
 * text — and, under it, the review actions (SubmissionReviewActions).
 *
 * Presentational, with one exception: a QUESTION or MATRIX fetches its own
 * authored config — option letters and row/column labels live there, not in
 * the payload — for the SELECTED reading only, so opening a submission costs no
 * fetch per object. Same key as the variant workspace, so a config already
 * seen there comes from cache. While it loads, the reading is shown with
 * indices instead of labels rather than not at all.
 */
export function SubmissionReviewFocus({
  obj, page, candidatePage, candidateListed, threshold, canAct, busy, editorError, onAction, actionsRef,
}: Props) {
  const needsConfig = obj.result_type === 'QUESTION' || obj.result_type === 'MATRIX'
  const configQ = useQuery({
    queryKey: ['page-object', obj.page_object_id],
    queryFn: () => nd.getPageObject(obj.page_object_id),
    enabled: needsConfig,
  })

  const lines = formatProposedReading(obj, needsConfig ? configQ.data : undefined)
  const explanations = obj.flag_reasons
    .map(code => reasonExplanation(code, threshold))
    .filter(text => text !== '')
  const where = page.template_page ? `Page ${page.template_page.page_index}` : 'Unplaced sheet'
  const candidate = candidateNote(obj, candidatePage, candidateListed)

  return (
    <div className="card">
      <div className="nd-review-focus-title">
        <h3>
          {objectTitle(obj)} <span className="nd-muted nd-review-focus-where">· {where}</span>
        </h3>
        {obj.flag_reasons.length > 0 && <ReasonChips reasons={obj.flag_reasons} />}
      </div>

      {/* A listed reading is always on a placed sheet; guarded all the same,
          since without a template page there is nothing to map it onto. */}
      {page.template_page && (
        <SubmissionScanCrop
          key={obj.result_id}
          pageId={page.id}
          alignmentStatus={page.alignment_status}
          geometry={obj.geometry}
          templatePage={page.template_page}
          objectTitle={objectTitle(obj)}
        />
      )}

      <dl className="nd-review-reading">
        {obj.question_text && (
          <>
            <dt>Question</dt>
            <dd>{obj.question_text}</dd>
          </>
        )}
        <dt>Proposed reading</dt>
        {lines.map((line, i) => <dd key={i}>{line}</dd>)}
        {/* Omitted when empty: a reading resolved before the threshold moved
            may have no reason left under today's threshold. */}
        {explanations.length > 0 && (
          <>
            <dt>Why it is here</dt>
            {explanations.map((text, i) => <dd key={i}>{text}</dd>)}
          </>
        )}
        {candidate && (
          <>
            <dt>Candidate</dt>
            <dd>{candidate}</dd>
          </>
        )}
      </dl>

      {/* Keyed by the reading AND its ruling: a new selection, or a save that
          changed the ruling, starts the action area afresh (editor closed,
          note reset to the current one). A refused save changes neither, so
          the editor stays open with its error. */}
      <SubmissionReviewActions
        key={`${obj.result_id}|${obj.review_outcome ?? ''}|${obj.reviewed_at ?? ''}`}
        obj={obj}
        config={needsConfig ? configQ.data : undefined}
        canAct={canAct}
        busy={busy}
        editorError={editorError}
        onAction={onAction}
        handleRef={actionsRef}
      />
    </div>
  )
}
