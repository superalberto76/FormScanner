// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AssignVariantModal } from '../components/AssignVariantModal'
import { BatchExportControl } from '../components/BatchExportControl'
import { BatchExtractionControl } from '../components/BatchExtractionControl'
import { BatchSubmissionsList } from '../components/BatchSubmissionsList'
import { BatchThresholdControl } from '../components/BatchThresholdControl'
import { BatchUploadArea } from '../components/BatchUploadArea'
import { Layout } from '../components/Layout'
import { pluralize } from '../components/batchUpload'
import {
  bucketFromParam,
  bucketToParam,
  markUnresolvableConsequence,
  truncateSubmissionId,
  unresolvableUndoneToast,
  type SubmissionBucket,
} from '../components/batchSubmissions'
import { useConfirm } from '../components/ConfirmProvider'
import { useToast } from '../components/ToastProvider'
import { normalizeApiError } from '../utils/apiError'
import { useAuth } from '../auth/authStore'
import { nd, type SubmissionSummary } from '../api/domain'

/**
 * Landing page after a batch is created, and the batch's review queue: a
 * header toolbar (run extraction, confidence threshold, export and
 * refresh), its submissions, and the ingestion area that adds
 * more of them. This page owns every query the list and the upload area need
 * — both presentational — so the list resolves variant names without a fetch
 * per row. The toolbar controls
 * own their own mutations.
 */
export function SubmissionBatchDetailPage() {
  const { projectId = '', batchId = '' } = useParams()
  // Only so the list can label the signed-in user's own review locks "You".
  const { user } = useAuth()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const confirm = useConfirm()
  const toast = useToast()

  // The active filter lives in the URL, so opening a submission and
  // coming back — or reloading, or sharing the link — keeps the operator's
  // view. Changing it REPLACES the history entry: clicking through four pills
  // must not take four Back presses to undo.
  const [searchParams, setSearchParams] = useSearchParams()
  const filterParam = searchParams.get('filter')
  const bucket = bucketFromParam(filterParam)
  function changeBucket(next: SubmissionBucket) {
    setSearchParams(prev => {
      const params = new URLSearchParams(prev)
      const slug = bucketToParam(next)
      if (slug) params.set('filter', slug)
      else params.delete('filter')
      return params
    }, { replace: true })
  }
  // Carried onto the review page, so its "Next submission" and its way back
  // stay inside the same filter.
  const filterSuffix = filterParam ? `?filter=${encodeURIComponent(filterParam)}` : ''
  const reviewPath = (submissionId: string) =>
    `/projects/${projectId}/scanning/batches/${batchId}/submissions/${submissionId}${filterSuffix}`

  // "Review anyway": confirmed, pessimistic, then straight into the
  // submission it just made reviewable.
  const reviewAnyway = useMutation({
    mutationFn: (submissionId: string) => nd.reviewSubmissionAnyway(submissionId),
    onSuccess: (_result, submissionId) => {
      toast.success('Submission ready for review')
      void qc.invalidateQueries({ queryKey: ['submission-batch', batchId] })
      void qc.invalidateQueries({ queryKey: ['submission-batches', projectId] })
      navigate(reviewPath(submissionId))
    },
    onError: err => toast.error(err),
  })

  async function confirmReviewAnyway(submission: SubmissionSummary) {
    const n = submission.unread_object_count
    const ok = await confirm({
      title: 'Review anyway',
      message:
        `${n} ${n === 1 ? 'object' : 'objects'} of this submission could not be read. ` +
        'If you have not done so, run extraction again first. ' +
        'Otherwise review anyway: you will enter those answers from the paper sheet.',
      confirmLabel: 'Review anyway',
    })
    if (ok) reviewAnyway.mutate(submission.id)
  }

  // Mark unresolvable / undo (from UNASSIGNED too): pessimistic, one mutation for both
  // directions, and every row's mark / undo button is disabled while it is
  // pending, so only one call is ever in flight. Settled either way, the batch
  // and the project's batch list are invalidated once — a 409 INVALID_STATUS
  // means another operator acted first, and the row must show what they did.
  const unresolvable = useMutation({
    mutationFn: ({ submissionId, undo }: { submissionId: string; undo: boolean }) =>
      undo
        ? nd.undoSubmissionUnresolvable(submissionId)
        : nd.markSubmissionUnresolvable(submissionId),
    onSuccess: (result, { undo }) => {
      // The undo toast reads the status the server returned the submission to.
      toast.success(undo ? unresolvableUndoneToast(result.status) : 'Submission marked unresolvable')
    },
    onError: err => toast.error(err),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ['submission-batch', batchId] })
      void qc.invalidateQueries({ queryKey: ['submission-batches', projectId] })
    },
  })

  async function confirmMarkUnresolvable(submission: SubmissionSummary) {
    const ok = await confirm({
      title: 'Mark as unresolvable',
      message: (
        <>
          Submission <code>{truncateSubmissionId(submission.id)}</code>{' '}
          {markUnresolvableConsequence(submission.status)}
        </>
      ),
      details: (
        <p className="nd-muted" style={{ fontSize: '0.8rem' }}>
          You can undo this later from the “Unresolvable” filter.
        </p>
      ),
      confirmLabel: 'Mark unresolvable',
      danger: true,
    })
    if (ok) unresolvable.mutate({ submissionId: submission.id, undo: false })
  }

  const batchQ = useQuery({
    queryKey: ['submission-batch', batchId],
    queryFn: () => nd.getSubmissionBatch(batchId),
    enabled: !!batchId,
    // On THIS query only (the global default stays off): the queue is shared,
    // and coming back to the tab should show other operators' progress —
    // without polling a list that can run into the hundreds.
    refetchOnWindowFocus: true,
  })

  // The middle crumb needs the project's name, which isn't on the batch payload
  // and isn't reliably in navigation state (a bookmarked/reloaded URL has none).
  // react-query dedupes this against the project detail page's identical key.
  const projectQ = useQuery({
    queryKey: ['project', projectId],
    queryFn: () => nd.getProject(projectId),
    enabled: !!projectId,
  })

  // ONE fetch for the whole list's variant column: a submission carries only its
  // matched variant's id, and resolving that per row would be an N+1 over a list
  // that is expected to run into the hundreds. Same key as the project detail
  // page, so react-query serves it from cache when arriving from there.
  const variantsQ = useQuery({
    queryKey: ['variants', projectId],
    queryFn: () => nd.listVariants(projectId),
    enabled: !!projectId,
  })

  const variantNameById = useMemo(
    () => new Map((variantsQ.data ?? []).map(v => [v.id, v.name])),
    [variantsQ.data],
  )

  // The assignment dropdown, filtered out of the SAME fetch the variant column
  // uses — a manual assignment is only ever to a PUBLISHED variant (the BE 409s
  // otherwise), and offering a DRAFT would be offering a guaranteed failure.
  const publishedVariants = useMemo(
    () => (variantsQ.data ?? []).filter(v => v.status === 'PUBLISHED'),
    [variantsQ.data],
  )

  // Selection and modal live here rather than in the list: the list stays
  // presentational, and clearing the ticks when the modal closes is then a plain
  // state update instead of a signal pushed back down into it.
  const [selectedIds, setSelectedIds] = useState<ReadonlySet<string>>(() => new Set())
  const [assignTargets, setAssignTargets] = useState<string[] | null>(null)

  function toggleSelected(submissionId: string) {
    setSelectedIds(prev => {
      const next = new Set(prev)
      if (!next.delete(submissionId)) next.add(submissionId)
      return next
    })
  }

  // Cancelled or completed, the ticks go: after an assignment they name submissions
  // that are no longer MATCH_FAILED, and leaving them ticked would invite a second
  // pointless assignment on rows that have already moved on.
  function closeAssign() {
    setAssignTargets(null)
    setSelectedIds(new Set())
  }

  const submissions = batchQ.data?.submissions ?? []
  const hasBatch = !!batchQ.data

  // Upload is ACTIVE-only on the BE; fail closed while the project loads. The
  // toolbar, "Assign variant" and "Mark / Undo unresolvable" are NOT gated: the BE allows them on a
  // CLOSED/ARCHIVED project, which is precisely when stragglers get resolved.
  const canCollect = projectQ.data?.status === 'ACTIVE'

  // null until the batch first arrives; then decided ONCE — expanded on an
  // empty batch (the dropzone is its empty state), collapsed otherwise — and
  // changed only by the user afterwards. Re-deriving it from the live count
  // would fold the area away under an operator whose first upload just
  // created the first submissions.
  const [uploadExpanded, setUploadExpanded] = useState<boolean | null>(null)
  // Once opened, the upload area stays mounted and is only hidden, so an
  // in-flight upload keeps its per-file rows when the user clicks "Hide".
  const [uploadMounted, setUploadMounted] = useState(false)
  const initialEmpty = hasBatch && submissions.length === 0
  useEffect(() => {
    if (!hasBatch || uploadExpanded !== null) return
    setUploadExpanded(initialEmpty)
    if (initialEmpty) setUploadMounted(true)
  }, [hasBatch, initialEmpty, uploadExpanded])

  function expandUpload() {
    setUploadExpanded(true)
    setUploadMounted(true)
  }

  return (
    <Layout>
      <p className="nd-breadcrumb mb-16">
        <Link to="/projects">Projects</Link> <span className="nd-muted">/</span>{' '}
        <Link to={`/projects/${projectId}/scanning`}>{projectQ.data?.name ?? '…'}</Link>{' '}
        <span className="nd-muted">/</span> {batchQ.data?.name ?? '…'}
      </p>

      {batchQ.isLoading && <p className="info-msg">Loading batch…</p>}
      {batchQ.isError && <p className="error-msg">{normalizeApiError(batchQ.error)}</p>}

      {batchQ.data && (
        <>
          <div className="card mb-16">
            <div className="row" style={{ justifyContent: 'space-between', alignItems: 'flex-start' }}>
              <h1 className="mb-16">{batchQ.data.name}</h1>
              <div className="row row-8">
                <span className="badge badge-archived">{pluralize(submissions.length, 'submission')}</span>
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  disabled={batchQ.isFetching}
                  onClick={() => void batchQ.refetch()}
                >
                  ↻ Refresh
                </button>
              </div>
            </div>
            {batchQ.data.description && <p className="nd-muted">{batchQ.data.description}</p>}
            <div className="nd-queue-toolbar">
              <BatchExtractionControl
                batchId={batchId}
                projectId={projectId}
                disabled={submissions.length === 0}
              />
              <BatchThresholdControl batch={batchQ.data} projectId={projectId} />
              <BatchExportControl
                batchId={batchId}
                batchName={batchQ.data.name}
                readiness={batchQ.data.export_readiness}
                submissionCount={submissions.length}
              />
            </div>
          </div>

          {/* Nothing uploaded yet: the dropzone IS the empty state, so an empty
              table above it would only repeat what it already says. */}
          {submissions.length > 0 && (
            <BatchSubmissionsList
              submissions={submissions}
              variantNameById={variantNameById}
              selectedIds={selectedIds}
              onToggleSelected={toggleSelected}
              onAssign={setAssignTargets}
              currentUserId={user?.id}
              onOpen={submissionId => navigate(reviewPath(submissionId))}
              bucket={bucket}
              onBucketChange={changeBucket}
              onReviewAnyway={submission => void confirmReviewAnyway(submission)}
              reviewAnywayPendingId={reviewAnyway.isPending ? reviewAnyway.variables ?? null : null}
              onMarkUnresolvable={submission => void confirmMarkUnresolvable(submission)}
              // No confirmation: undoing only reopens the submission for assignment.
              onUndoUnresolvable={submission =>
                unresolvable.mutate({ submissionId: submission.id, undo: true })}
              unresolvablePendingId={
                unresolvable.isPending ? unresolvable.variables?.submissionId ?? null : null
              }
              unresolvableBusy={unresolvable.isPending}
            />
          )}

          {/* Stays below the list rather than being replaced by it: more sheets
              can be added to a batch at any point. On a batch that already has
              submissions it starts collapsed behind "+ Add files", so the queue
              stays the first thing on the page. */}
          {canCollect && uploadExpanded === false && (
            <div className="card mb-16">
              <div className="nd-add-files-row">
                <span className="nd-muted">Add more scanned sheets to this batch.</span>
                <button type="button" className="btn btn-secondary btn-sm" onClick={expandUpload}>
                  + Add files
                </button>
              </div>
            </div>
          )}
          {canCollect && uploadMounted && (
            <div hidden={!uploadExpanded}>
              {/* No way to hide the empty state: with nothing uploaded, the
                  dropzone is the only thing this page has to offer. */}
              {submissions.length > 0 && (
                <div className="nd-add-files-row mb-8">
                  <h3>Add files</h3>
                  <button type="button" className="nd-link-btn" onClick={() => setUploadExpanded(false)}>
                    Hide
                  </button>
                </div>
              )}
              <BatchUploadArea batchId={batchId} />
            </div>
          )}

          {/* Mounted by the page, not by the list, so it can invalidate the batch
              query this page owns once the assignment is over. */}
          {assignTargets && (
            <AssignVariantModal
              submissionIds={assignTargets}
              publishedVariants={publishedVariants}
              batchId={batchId}
              onClose={closeAssign}
            />
          )}
        </>
      )}
    </Layout>
  )
}
