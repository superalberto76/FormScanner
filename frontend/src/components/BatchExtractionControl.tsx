// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { nd, type ExtractionRunSummary } from '../api/domain'
import { extractionResultLines } from './batchQueue'
import { useToast } from './ToastProvider'

interface Props {
  batchId: string
  projectId: string
  /** True when the batch has no submissions yet: there is nothing to read. */
  disabled: boolean
}

/**
 * "Run extraction" in the review queue's toolbar.
 *
 * Owns its own mutation, like BatchUploadArea: the page only places it. The
 * call is pessimistic and can take minutes on a large batch, so the button says
 * what is happening and asks the operator to keep the page open — nothing is
 * shown as done before the BE says so.
 *
 * The result of the LAST run in this session is kept in local state and never
 * persisted: it describes a call, not the batch, and the batch's own state is
 * what the list below shows once the queries are invalidated.
 */
export function BatchExtractionControl({ batchId, projectId, disabled }: Props) {
  const qc = useQueryClient()
  const toast = useToast()
  const [lastRun, setLastRun] = useState<ExtractionRunSummary | null>(null)

  const runM = useMutation({
    mutationFn: () => nd.runBatchExtraction(batchId),
    onSuccess: summary => {
      setLastRun(summary)
      // One invalidation for the whole run: statuses move to SCANNED and the
      // pending counts change, both in this batch and in the project's list.
      void qc.invalidateQueries({ queryKey: ['submission-batch', batchId] })
      void qc.invalidateQueries({ queryKey: ['submission-batches', projectId] })
    },
    // The previous result stays: it is still a true account of that run.
    onError: err => toast.error(err),
  })

  const lines = lastRun ? extractionResultLines(lastRun) : null

  return (
    <div className="nd-tool">
      <span className="nd-tool-label">Extraction</span>
      <div>
        <button
          type="button"
          className="btn btn-primary btn-sm"
          disabled={disabled || runM.isPending}
          title={disabled ? 'Upload submissions first' : undefined}
          onClick={() => runM.mutate()}
        >
          {runM.isPending ? 'Running extraction…' : 'Run extraction'}
        </button>
      </div>
      {runM.isPending ? (
        <span className="nd-run-result">
          This may take a few minutes for a large batch. Keep this page open.
        </span>
      ) : (
        lines && (
          <>
            <span className="nd-run-result">{lines.main}</span>
            {lines.errors && <span className="nd-run-result has-errors">{lines.errors}</span>}
          </>
        )
      )}
    </div>
  )
}
