// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { nd, type SubmissionBatchRead } from '../api/domain'
import { normalizeApiError } from '../utils/apiError'
import { formatThresholdPercent, parseThresholdPercentInput } from './batchQueue'

interface Props {
  batch: Pick<SubmissionBatchRead, 'id' | 'confidence_threshold' | 'effective_threshold'>
  projectId: string
}

/**
 * The batch's confidence threshold in the review queue's toolbar.
 *
 * Read mode shows the threshold in force and where it comes from; edit mode
 * takes an integer percent and sends the fraction. Owns its own mutation, like
 * BatchExtractionControl — separate components on purpose, since the two share
 * nothing but a toolbar row.
 *
 * "Reset to system default" names no number: while a custom value is set the
 * batch payload carries only the override and the effective value (which IS
 * the override), so the default's value is simply not known here.
 */
export function BatchThresholdControl({ batch, projectId }: Props) {
  const qc = useQueryClient()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')

  const isCustom = batch.confidence_threshold !== null

  const saveM = useMutation({
    mutationFn: (threshold: number | null) =>
      nd.updateBatchConfidenceThreshold(batch.id, threshold),
    onSuccess: () => {
      setEditing(false)
      // One invalidation: moving the threshold changes which results need
      // review, so the "In review" counts change here and in the project list.
      void qc.invalidateQueries({ queryKey: ['submission-batch', batch.id] })
      void qc.invalidateQueries({ queryKey: ['submission-batches', projectId] })
    },
    // No onError: the failure is shown inline and the typed value is kept.
  })

  function startEdit() {
    // Whole percent: the input takes integers only, so a fractional override
    // set elsewhere (82.5%) is offered rounded rather than as an invalid value.
    setDraft(String(Math.round(batch.effective_threshold * 100)))
    saveM.reset()
    setEditing(true)
  }

  if (!editing) {
    return (
      <div className="nd-tool">
        <span className="nd-tool-label">Confidence threshold</span>
        <div className="row row-8">
          <span className="nd-threshold-value">{formatThresholdPercent(batch.effective_threshold)}</span>
          <span className="nd-threshold-origin">{isCustom ? 'custom' : 'system default'}</span>
          <button type="button" className="nd-link-btn" onClick={startEdit}>Edit</button>
        </div>
        <span className="nd-run-result">Readings below this confidence are sent to review.</span>
      </div>
    )
  }

  const parsed = parseThresholdPercentInput(draft)

  return (
    <div className="nd-tool">
      <label className="nd-tool-label" htmlFor="batch-threshold-input">Confidence threshold</label>
      <form
        className="row row-8"
        onSubmit={e => {
          e.preventDefault()
          if (parsed !== null && !saveM.isPending) saveM.mutate(parsed)
        }}
      >
        <input
          id="batch-threshold-input"
          className="nd-threshold-input"
          type="number"
          min={0}
          max={100}
          step={1}
          value={draft}
          autoFocus
          onChange={e => setDraft(e.target.value)}
        />
        <span>%</span>
        <button
          type="submit"
          className="btn btn-primary btn-sm"
          disabled={parsed === null || saveM.isPending}
        >
          Save
        </button>
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          disabled={saveM.isPending}
          onClick={() => setEditing(false)}
        >
          Cancel
        </button>
      </form>
      {isCustom && (
        <button
          type="button"
          className="nd-link-btn"
          style={{ alignSelf: 'flex-start' }}
          disabled={saveM.isPending}
          onClick={() => saveM.mutate(null)}
        >
          Reset to system default
        </button>
      )}
      {saveM.isError && <p className="error-msg">{normalizeApiError(saveM.error)}</p>}
    </div>
  )
}
