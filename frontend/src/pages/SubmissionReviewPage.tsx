// SPDX-FileCopyrightText: 2026 Alberto Borsetta
// SPDX-License-Identifier: AGPL-3.0-or-later
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type MutableRefObject,
  type ReactNode,
} from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Layout } from '../components/Layout'
import { SubmissionReviewFocus } from '../components/SubmissionReviewFocus'
import { SubmissionReviewList } from '../components/SubmissionReviewList'
import {
  bucketFromParam,
  formatLockAge,
  lockHolderLabel,
  missingPagesLabel,
  nextSubmissionId,
  showNoCandidate,
  submissionBadgeClass,
  truncateSubmissionId,
} from '../components/batchSubmissions'
import {
  adjacentResultId,
  canReopen,
  closeWarnings,
  defaultSelection,
  lockView,
  nextUnresolvedResultId,
  reviewCountLine,
  reviewKeyAction,
  type AcquireState,
} from '../components/submissionReview'
import type { ReviewActionsHandle } from '../components/SubmissionReviewActions'
import { useConfirm } from '../components/ConfirmProvider'
import { useToast } from '../components/ToastProvider'
import {
  nd,
  releaseSubmissionLockOnUnload,
  type ReviewActionType,
  type SubmissionLockInfo,
  type SubmissionLockState,
  type SubmissionReviewDetail,
  type SubstituteValue,
} from '../api/domain'
import { useAuth } from '../auth/authStore'
import { getErrorCode, getStatus, isNotFound, normalizeApiError } from '../utils/apiError'
import { formatScoreTriple } from '../utils/scoreFormat'

const NOT_REVIEWABLE = 'NOT_REVIEWABLE'
const LOCK_LOST = 'LOCK_LOST'
const SUBMISSION_CHANGED = 'SUBMISSION_CHANGED'
// The refusals of a review action.
const NOT_CONFIRMABLE = 'NOT_CONFIRMABLE'
const NOT_RESOLVED = 'NOT_RESOLVED'
// The refusal of a reopen: the submission is no longer REVIEWED.
const INVALID_STATUS = 'INVALID_STATUS'

type LockChain = MutableRefObject<Promise<unknown>>

/**
 * The route element. Renders the page KEYED by the submission id, so "Next
 * submission" starts from a clean slate — selection, edit mode,
 * lock outcome — instead of carrying one submission's state into the next.
 *
 * The lock-call chain lives HERE, above the key, so it survives that remount:
 * the previous submission's release and the next one's acquire still go out one
 * after the other, through the same chain as every other lock call.
 */
export function SubmissionReviewPage() {
  const { submissionId = '' } = useParams()
  const lockChain = useRef<Promise<unknown>>(Promise.resolve())
  return <SubmissionReview key={submissionId} lockChain={lockChain} />
}

/**
 * One submission, as an operator reviews it: a header, the readings
 * that needed a human on the left, the selected one on the right.
 *
 * Reads one endpoint and holds the review LOCK:
 * acquired on open, released on leave, taken over on request — and only while
 * the submission is SCANNED; any other status is shown read-only with no lock
 * call at all. The list and the focus panel are presentational; this page owns
 * the query, the selection and every lock call.
 *
 * The manual candidate edit is a review save, with every save error routed
 * through :func:`handleSaveError`; "Next submission" walks the batch's queue
 * inside the filter the operator came from.
 *
 * The review actions on a reading (CONFIRM / SUBSTITUTE / FLAG / UNDO, from
 * the focus panel) and "Accept all remaining" (from the list header) are two
 * pessimistic mutations owned here, whose responses — the whole review —
 * replace the cached one. The keyboard drives them too: one document
 * listener, which asks `reviewKeyAction` what a key means and calls the very
 * handler the matching button runs.
 *
 * "Close submission" in the header is one more pessimistic review save,
 * confirmed first when something is worth seeing (`closeWarnings`). Its
 * response is the closed submission, so the page turns read-only in place.
 * "Reopen submission" sits in the same place on a REVIEWED one, with no
 * confirmation: its response is the open submission, the lock already ours.
 */
function SubmissionReview({ lockChain }: { lockChain: LockChain }) {
  const { projectId = '', batchId = '', submissionId = '' } = useParams()
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const { user } = useAuth()
  const qc = useQueryClient()
  const toast = useToast()
  const confirm = useConfirm()

  // The queue filter the operator came from, carried on every link
  // back to the queue and on to the next submission.
  const filterParam = searchParams.get('filter')
  const filterSuffix = filterParam ? `?filter=${encodeURIComponent(filterParam)}` : ''

  const reviewKey = useMemo(() => ['submission-review', submissionId], [submissionId])
  const reviewQ = useQuery({
    queryKey: reviewKey,
    queryFn: () => nd.getSubmissionReview(submissionId),
    enabled: !!submissionId,
    // The submission is shared: coming back to the tab must show a colleague's
    // take-over. Invalidation and focus only — never polling.
    refetchOnWindowFocus: true,
  })
  // Same key as the batch page, so arriving from there costs nothing.
  const projectQ = useQuery({
    queryKey: ['project', projectId],
    queryFn: () => nd.getProject(projectId),
    enabled: !!projectId,
  })

  const detail = reviewQ.data
  const isScanned = detail?.status === 'SCANNED'

  // ── Lock ───────────────────────────────────────────────────────────────────
  //
  // Every lock call of this page goes through ONE promise chain, so each starts
  // only after the previous one has answered. React StrictMode mounts, unmounts
  // and remounts in dev: without the chain, the acquire → release → acquire it
  // fires could reach the server out of order and leave the submission unlocked
  // under an operator who believes they hold it. The chain lives in the route
  // wrapper's ref, which survives that simulated remount AND the keyed remount
  // of "Next submission".
  const enqueueLockCall = useCallback(<T,>(call: () => Promise<T>): Promise<T> => {
    // Runs after the previous call whatever its outcome; the chain itself
    // never rejects, so one failure does not block every later call.
    const next = lockChain.current.then(call, call)
    lockChain.current = next.catch(() => undefined)
    return next
  }, [lockChain])

  const [acquire, setAcquire] = useState<AcquireState>({ settled: false })
  // Whether this page has ever held the lock: what tells a colleague's lock
  // that TOOK it from us ("lost") from one that was there when we arrived
  // ("held").
  const [everAcquired, setEverAcquired] = useState(false)
  // The holder named by our last refused acquire, for the banner while the
  // review payload has not caught up.
  const [refusedBy, setRefusedBy] = useState<SubmissionLockInfo | null>(null)
  const [notReviewable, setNotReviewable] = useState(false)

  // Writes the lock a lock call just reported into the cached review, so the
  // banner reflects the call at once rather than a payload fetched before it.
  // Not a substitute for the refetch the callers trigger.
  const applyLockState = useCallback((state: SubmissionLockState) => {
    qc.setQueryData<SubmissionReviewDetail>(reviewKey, old =>
      old ? { ...old, lock: state.lock, review_version: state.review_version } : old,
    )
  }, [qc, reviewKey])

  const handleNotReviewable = useCallback((err: unknown) => {
    // The submission left SCANNED under us: read-only, and refetch to learn
    // its real status.
    setNotReviewable(true)
    toast.info(normalizeApiError(err))
    void qc.invalidateQueries({ queryKey: reviewKey })
  }, [qc, reviewKey, toast])

  // The lock effect must run on open and on leave ONLY — re-running it would
  // release and re-acquire. `toast` is a new object every render and the two
  // handlers depend on it, so the effect reads them through this ref instead
  // of listing them as dependencies.
  const handlers = useRef({ applyLockState, handleNotReviewable, toast })
  handlers.current = { applyLockState, handleNotReviewable, toast }

  useEffect(() => {
    if (!isScanned) return
    let current = true

    const acquireLock = () => {
      setAcquire({ settled: false })
      enqueueLockCall(() => nd.acquireSubmissionLock(submissionId))
        .then(state => {
          if (!current) return
          handlers.current.applyLockState(state)
          setAcquire({ settled: true, acquired: state.acquired })
          if (state.acquired) setEverAcquired(true)
          else setRefusedBy(state.lock)
        })
        .catch(err => {
          if (!current) return
          setAcquire({ settled: true, acquired: false })
          if (getErrorCode(err) === NOT_REVIEWABLE) handlers.current.handleNotReviewable(err)
          else handlers.current.toast.error(err, 'Could not open this submission for review.')
        })
    }
    acquireLock()

    // A tab being closed or navigated away never runs the cleanup below.
    const onPageHide = () => releaseSubmissionLockOnUnload(submissionId)
    // Restored from the back/forward cache: the lock was released on pagehide,
    // so take it again rather than show a lock we no longer hold.
    const onPageShow = (e: PageTransitionEvent) => { if (e.persisted) acquireLock() }
    window.addEventListener('pagehide', onPageHide)
    window.addEventListener('pageshow', onPageShow)

    return () => {
      current = false
      window.removeEventListener('pagehide', onPageHide)
      window.removeEventListener('pageshow', onPageShow)
      enqueueLockCall(() => nd.releaseSubmissionLock(submissionId))
        .catch(() => undefined)
        // ONE invalidation, after the release has landed: the queue's
        // "In review by" must not show us still holding it.
        .finally(() => { void qc.invalidateQueries({ queryKey: ['submission-batch', batchId] }) })
    }
  }, [isScanned, submissionId, batchId, enqueueLockCall, qc])

  const takeOver = useMutation({
    mutationFn: () => enqueueLockCall(() => nd.takeOverSubmissionLock(submissionId)),
    onSuccess: state => {
      applyLockState(state)
      setAcquire({ settled: true, acquired: true })
      setEverAcquired(true)
      setRefusedBy(null)
      void qc.invalidateQueries({ queryKey: reviewKey })
      void qc.invalidateQueries({ queryKey: ['submission-batch', batchId] })
    },
    onError: err => {
      if (getErrorCode(err) === NOT_REVIEWABLE) handleNotReviewable(err)
      else toast.error(err, 'Could not take over this submission.')
    },
  })

  // ── Review saves (the review actions reuse this) ───────────────────────────

  /**
   * THE single entry point for a refused review save on this page. Every save
   * goes through the BE's review-save guard, so every save can be refused for the
   * same three reasons, and each gets one reaction here rather than one per
   * call site:
   *
   *  - LOCK_LOST — a colleague took the submission: say so, refetch; the
   *    "took over" banner then appears through the lock view;
   *  - SUBMISSION_CHANGED — the screen was stale: say so, refetch;
   *  - NOT_REVIEWABLE — it left SCANNED: say so, refetch to learn its status.
   *
   * The two refusals of a review action both mean the
   * screen no longer matches the reading's state — say so, refetch:
   *
   *  - NOT_CONFIRMABLE — the reading cannot be confirmed (the button is only
   *    offered when the server said it could, so the data moved);
   *  - NOT_RESOLVED — Undo of a reading that is no longer resolved.
   *
   * The reopen's own refusal gets the same reaction:
   *
   *  - INVALID_STATUS — the submission is no longer REVIEWED (a colleague
   *    reopened it first).
   *
   * A 422 is NOT handled here: it is about what was typed, so the caller reads
   * it off its own mutation's error and shows it inline next to the input (the
   * candidate field, the replacement editor). Anything else is a plain error
   * toast.
   */
  const handleSaveError = useCallback((err: unknown) => {
    if (getStatus(err) === 422) return
    switch (getErrorCode(err)) {
      case LOCK_LOST:
      case NOT_REVIEWABLE:
      case NOT_CONFIRMABLE:
      case NOT_RESOLVED:
      case INVALID_STATUS:
        toast.error(normalizeApiError(err))
        void qc.invalidateQueries({ queryKey: reviewKey })
        return
      case SUBMISSION_CHANGED:
        toast.error('This submission was changed by someone else. It has been reloaded.')
        void qc.invalidateQueries({ queryKey: reviewKey })
        return
      default:
        toast.error(err, 'Could not save.')
    }
  }, [qc, reviewKey, toast])

  const [editingCandidate, setEditingCandidate] = useState(false)
  const editCandidate = useMutation({
    mutationFn: (value: string | null) =>
      nd.editSubmissionCandidate(submissionId, value, detail?.review_version ?? 0),
    onSuccess: state => {
      // The SERVER's answer, not the typed value: it is what was stored
      // (trimmed, blank → null), with the version the next save must declare.
      qc.setQueryData<SubmissionReviewDetail>(reviewKey, old =>
        old
          ? {
              ...old,
              respondent_identifier: state.respondent_identifier,
              review_version: state.review_version,
              lock: state.lock,
            }
          : old,
      )
      setEditingCandidate(false)
      // The queue shows the candidate.
      void qc.invalidateQueries({ queryKey: ['submission-batch', batchId] })
    },
    onError: err => {
      // A refused save leaves edit mode: after the refetch the value — or
      // who may edit it — may have changed. A 422 keeps it, to fix the input.
      if (getStatus(err) !== 422) setEditingCandidate(false)
      handleSaveError(err)
    },
  })

  // ── Review actions ─────────────────────────────────────────────────────────
  //
  // Both pessimistic: every action control is disabled while either is in
  // flight, and the cache is only ever written with the server's response —
  // the whole review — followed by ONE invalidation of the queue, whose counts
  // the action changed.

  const [selectedId, setSelectedId] = useState<string | null>(null)

  const reviewAction = useMutation({
    mutationFn: (v: {
      resultId: string
      action: ReviewActionType
      note: string | null
      value?: SubstituteValue
      version: number
    }) =>
      nd.reviewResult(submissionId, v.resultId, {
        action: v.action,
        review_version: v.version,
        note: v.note,
        value: v.value ?? null,
      }),
    onSuccess: (next, v) => {
      qc.setQueryData<SubmissionReviewDetail>(reviewKey, next)
      void qc.invalidateQueries({ queryKey: ['submission-batch', batchId] })
      // A ruling moves on to the next reading needing one; Undo stays, since
      // the operator is about to rule on it again. With nothing left, the
      // selection is pinned where it is rather than jumping to the default.
      if (v.action !== 'UNDO') setSelectedId(nextUnresolvedResultId(next, v.resultId) ?? v.resultId)
    },
    onError: handleSaveError,
  })

  const acceptRemaining = useMutation({
    mutationFn: (version: number) => nd.acceptRemaining(submissionId, version),
    onSuccess: ({ accepted_count, ...next }) => {
      qc.setQueryData<SubmissionReviewDetail>(reviewKey, next)
      toast.success(`${accepted_count} ${accepted_count === 1 ? 'reading' : 'readings'} confirmed`)
      void qc.invalidateQueries({ queryKey: ['submission-batch', batchId] })
      const current = selectedRef.current
      setSelectedId(nextUnresolvedResultId(next, current) ?? current)
    },
    onError: handleSaveError,
  })

  // ── Close submission ───────────────────────────────────────────────────────
  //
  // A review save like the others: pessimistic, the response (the closed
  // submission) written to the cache, errors through handleSaveError — whose
  // default branch shows EXTRACTION_RUNNING's message as is. The status leaving
  // SCANNED re-runs the lock effect, whose cleanup releases (a no-op here) and
  // invalidates the batch; the project's batch list is invalidated here.
  const closeSubmission = useMutation({
    mutationFn: (version: number) => nd.closeSubmission(submissionId, version),
    onSuccess: next => {
      qc.setQueryData<SubmissionReviewDetail>(reviewKey, next)
      toast.success('Submission closed.')
      void qc.invalidateQueries({ queryKey: ['submission-batches', projectId] })
    },
    onError: handleSaveError,
  })

  // ── Reopen submission ──────────────────────────────────────────────────────
  //
  // Pessimistic, no confirmation: the response — SCANNED, the
  // lock ours — goes to the cache, and the status entering SCANNED re-runs the
  // lock effect, which renews that lock. Nothing invalidates the queue on the
  // way in, so both keys are invalidated here, once each. A NOT_REVIEWABLE
  // seen earlier (the submission closed under us) no longer applies.
  const reopenSubmission = useMutation({
    mutationFn: (version: number) => nd.reopenSubmission(submissionId, version),
    onSuccess: next => {
      qc.setQueryData<SubmissionReviewDetail>(reviewKey, next)
      setNotReviewable(false)
      toast.success('Submission reopened.')
      void qc.invalidateQueries({ queryKey: ['submission-batch', batchId] })
      void qc.invalidateQueries({ queryKey: ['submission-batches', projectId] })
    },
    onError: err => {
      handleSaveError(err)
      // Refused because a colleague got there first: the queue moved too, and
      // no lock cleanup will refresh it — the submission is still read-only
      // here until the refetch lands.
      void qc.invalidateQueries({ queryKey: ['submission-batch', batchId] })
    },
  })

  const actionsBusy =
    reviewAction.isPending || acceptRemaining.isPending || closeSubmission.isPending || reopenSubmission.isPending

  async function onClose() {
    if (!detail || actionsBusy) return
    // The version is read BEFORE the dialog, unlike "Accept all remaining":
    // the warnings describe this version, and the server must close exactly
    // what the operator confirmed — or refuse with SUBMISSION_CHANGED.
    const version = detail.review_version
    const warnings = closeWarnings(detail)
    if (warnings.length > 0) {
      const ok = await confirm({
        title: 'Close submission',
        message: `Close submission ${truncateSubmissionId(detail.id)}? It will become read-only.`,
        details: (
          <ul className="nd-close-warnings">
            {warnings.map(w => (
              <li key={w.title}>⚠ <b>{w.title}</b> {w.detail}</li>
            ))}
          </ul>
        ),
        confirmLabel: 'Close submission',
      })
      if (!ok) return
    }
    closeSubmission.mutate(version)
  }

  async function onAcceptRemaining() {
    if (!detail || actionsBusy) return
    const n = detail.bulk_acceptable_count
    const ok = await confirm({
      title: 'Accept all remaining',
      message:
        `Confirm the proposed reading of ${n} remaining ${n === 1 ? 'object' : 'objects'}? ` +
        'Handwritten text, identifiers with ambiguous or missing digits, codes that could ' +
        'not be decoded, readings that could not be extracted and readings on sheets that ' +
        'could not be aligned are left for you to review one by one.',
      confirmLabel: `Accept ${n}`,
    })
    // The version is read AFTER the dialog: a refetch meanwhile must not make
    // the save declare a stale one.
    const version = qc.getQueryData<SubmissionReviewDetail>(reviewKey)?.review_version
    if (ok && version !== undefined) acceptRemaining.mutate(version)
  }

  // ── Next submission ────────────────────────────────────────────────────────

  // The queue's own query, so arriving from the queue it is already cached,
  // and the lock invalidations this page fires keep it current.
  const batchQ = useQuery({
    queryKey: ['submission-batch', batchId],
    queryFn: () => nd.getSubmissionBatch(batchId),
    enabled: !!batchId,
  })
  const nextId = batchQ.data
    ? nextSubmissionId(batchQ.data.submissions, submissionId, bucketFromParam(filterParam), user?.id)
    : null

  // ── Selection ──────────────────────────────────────────────────────────────

  // A selection that no longer exists after a refetch falls back to the
  // default rather than to an empty focus.
  const located = useMemo(() => {
    if (!detail) return null
    const find = (id: string | null) => {
      for (const page of detail.pages) {
        const obj = page.objects.find(o => o.result_id === id)
        if (obj) return { page, obj }
      }
      return null
    }
    return find(selectedId) ?? find(defaultSelection(detail))
  }, [detail, selectedId])
  // The reading actually shown, for "Accept all remaining", whose success
  // handler runs after renders this closure has not seen.
  const selectedRef = useRef<string | null>(null)
  selectedRef.current = located?.obj.result_id ?? null

  function select(resultId: string) {
    // A 422 belongs to the reading it was made on; leaving it drops it.
    reviewAction.reset()
    setSelectedId(resultId)
  }

  // ── Render ─────────────────────────────────────────────────────────────────

  const shortId = truncateSubmissionId(submissionId)
  const view = !detail
    ? 'pending'
    : notReviewable && isScanned
      ? 'closed'
      : lockView({
          status: detail.status,
          acquire,
          detailLock: detail.lock,
          currentUserId: user?.id,
          everAcquired,
        })
  // The review actions: only with the lock in hand on a SCANNED submission —
  // anything else would be a save the guard refuses.
  const canAct = view === 'mine' && detail?.status === 'SCANNED'

  // ── Keyboard ───────────────────────────────────────────────────────────────
  //
  // ONE keydown listener on the document, registered once. It holds no rule:
  // `reviewKeyAction` decides, and each answer maps to an existing handler —
  // the action area's own (through its handle: same note, same mutation, same
  // next-selection as a click) or the selection. The latest render's values
  // are read through a ref, so the listener never has to be re-registered.
  const actionsRef = useRef<ReviewActionsHandle>(null)
  const keyState = useRef({ view, detail, located, actionsBusy, select })
  keyState.current = { view, detail, located, actionsBusy, select }

  useEffect(() => {
    function onKeyDown(e: globalThis.KeyboardEvent) {
      const { view, detail, located, actionsBusy, select } = keyState.current
      if (!detail) return
      const handle = actionsRef.current
      const target = e.target instanceof HTMLElement ? e.target : null
      const action = reviewKeyAction(
        {
          key: e.key,
          ctrlKey: e.ctrlKey,
          metaKey: e.metaKey,
          altKey: e.altKey,
          repeat: e.repeat,
          defaultPrevented: e.defaultPrevented,
          targetIsField:
            !!target && (target.isContentEditable || !!target.closest('input, textarea, select')),
          targetIsControl:
            !!target && !!target.closest('button, a[href], summary') && !target.closest('[data-review-row]'),
        },
        {
          lockMine: view === 'mine',
          scanned: detail.status === 'SCANNED',
          editorOpen: handle?.editorOpen ?? false,
          noteOpen: handle?.noteOpen ?? false,
          // Every modal of the app (ConfirmProvider's dialog included) renders
          // this backdrop; while one is up, its keys are its own.
          dialogOpen: document.querySelector('.modal-backdrop') !== null,
          pending: actionsBusy,
          selected: located
            ? { resolved: located.obj.resolved, confirmable: located.obj.confirmable }
            : null,
        },
      )
      if (action === null) return
      e.preventDefault()
      switch (action) {
        case 'confirm': handle?.act('CONFIRM'); break
        case 'flag': handle?.act('FLAG'); break
        case 'undo': handle?.act('UNDO'); break
        case 'replace': handle?.openEditor(); break
        case 'escape': handle?.closeOpen(); break
        case 'next':
        case 'prev': {
          const id = adjacentResultId(detail, located?.obj.result_id ?? null, action)
          if (id) select(id)
          break
        }
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [])

  return (
    <Layout>
      <p className="nd-breadcrumb mb-16">
        <Link to="/projects">Projects</Link> <span className="nd-muted">/</span>{' '}
        <Link to={`/projects/${projectId}/scanning`}>{projectQ.data?.name ?? '…'}</Link>{' '}
        <span className="nd-muted">/</span>{' '}
        <Link to={`/projects/${projectId}/scanning/batches/${batchId}${filterSuffix}`}>
          {detail?.batch_name ?? '…'}
        </Link>{' '}
        <span className="nd-muted">/</span> Submission {shortId}
      </p>

      {reviewQ.isLoading && <p className="info-msg">Loading submission…</p>}
      {reviewQ.isError && (
        <p className="error-msg">
          {isNotFound(reviewQ.error) ? 'Submission not found.' : normalizeApiError(reviewQ.error)}
        </p>
      )}

      {detail && (
        <>
          <LockBanner
            view={view}
            lock={detail.lock ?? refusedBy}
            currentUserId={user?.id}
            takingOver={takeOver.isPending}
            onTakeOver={() => takeOver.mutate()}
          />

          <SubmissionHeader
            detail={detail}
            shortId={shortId}
            refreshing={reviewQ.isFetching}
            onRefresh={() => void reviewQ.refetch()}
            nextId={nextId}
            close={
              detail.status === 'SCANNED'
                ? {
                    // Visible on every open submission, whatever is pending;
                    // usable only with the lock in hand.
                    enabled: canAct && !actionsBusy,
                    closing: closeSubmission.isPending,
                    hint: view === 'held' || view === 'lost' ? 'Take over to close this submission' : undefined,
                    onClick: () => void onClose(),
                  }
                : undefined
            }
            reopen={
              canReopen(detail.status)
                ? {
                    enabled: !actionsBusy,
                    reopening: reopenSubmission.isPending,
                    onClick: () => reopenSubmission.mutate(detail.review_version),
                  }
                : undefined
            }
            onNext={id => navigate(
              `/projects/${projectId}/scanning/batches/${batchId}/submissions/${id}${filterSuffix}`,
            )}
            candidate={
              <CandidateField
                detail={detail}
                // Editable only while the lock is ours: anything else would be
                // a save the guard refuses.
                canEdit={view === 'mine'}
                editing={editingCandidate && view === 'mine'}
                onEdit={() => { editCandidate.reset(); setEditingCandidate(true) }}
                onCancel={() => setEditingCandidate(false)}
                onSave={value => editCandidate.mutate(value)}
                saving={editCandidate.isPending}
                inlineError={
                  getStatus(editCandidate.error) === 422 ? normalizeApiError(editCandidate.error) : null
                }
              />
            }
          />

          <div className="nd-review-layout">
            <SubmissionReviewList
              detail={detail}
              selectedId={located?.obj.result_id ?? null}
              onSelect={select}
              acceptRemaining={
                canAct && detail.bulk_acceptable_count > 0
                  ? {
                      count: detail.bulk_acceptable_count,
                      busy: actionsBusy,
                      onClick: () => void onAcceptRemaining(),
                    }
                  : undefined
              }
            />
            {located && (
              <SubmissionReviewFocus
                obj={located.obj}
                page={located.page}
                candidatePage={
                  detail.pages.find(p => p.id === detail.candidate_reading_page_id) ?? null
                }
                candidateListed={detail.pages.some(p => p.objects.some(o => o.sets_candidate))}
                threshold={detail.effective_threshold}
                canAct={canAct}
                busy={actionsBusy}
                editorError={
                  getStatus(reviewAction.error) === 422 &&
                  reviewAction.variables?.resultId === located.obj.result_id
                    ? normalizeApiError(reviewAction.error)
                    : null
                }
                onAction={(action, note, value) => {
                  if (actionsBusy) return
                  reviewAction.mutate({
                    resultId: located.obj.result_id,
                    action,
                    note,
                    value,
                    version: detail.review_version,
                  })
                }}
                actionsRef={actionsRef}
              />
            )}
          </div>
        </>
      )}
    </Layout>
  )
}

function SubmissionHeader({
  detail, shortId, refreshing, onRefresh, nextId, close, reopen, onNext, candidate,
}: {
  detail: SubmissionReviewDetail
  shortId: string
  refreshing: boolean
  onRefresh: () => void
  /** The submission "Next submission" opens, or null when none qualifies. */
  nextId: string | null
  /** "Close submission"; absent when the submission is not open. */
  close?: { enabled: boolean; closing: boolean; hint?: string; onClick: () => void }
  /** "Reopen submission"; absent unless the submission is REVIEWED. */
  reopen?: { enabled: boolean; reopening: boolean; onClick: () => void }
  onNext: (submissionId: string) => void
  /** The Candidate value, with its editor (owned by the page). */
  candidate: ReactNode
}) {
  const placed = detail.pages.filter(p => p.template_page !== null).length
  const missing = missingPagesLabel(detail.missing_page_indices)
  const count = reviewCountLine(detail)
  return (
    <div className="card mb-16">
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'flex-start' }}>
        <div className="row row-8">
          <h1>Submission <code title={detail.id}>{shortId}</code></h1>
          <span className={`badge ${submissionBadgeClass(detail.status)}`}>{detail.status}</span>
        </div>
        <div className="row row-8">
          <button type="button" className="btn btn-secondary btn-sm" disabled={refreshing} onClick={onRefresh}>
            ↻ Refresh
          </button>
          {close && (
            <button
              type="button"
              className="btn btn-primary btn-sm"
              disabled={!close.enabled}
              title={close.hint}
              onClick={close.onClick}
            >
              {close.closing ? 'Closing…' : 'Close submission'}
            </button>
          )}
          {reopen && (
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              disabled={!reopen.enabled}
              onClick={reopen.onClick}
            >
              {reopen.reopening ? 'Reopening…' : 'Reopen submission'}
            </button>
          )}
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            disabled={nextId === null}
            title={nextId === null ? 'No other submission in this filter is left to review or close' : undefined}
            onClick={() => { if (nextId) onNext(nextId) }}
          >
            Next submission →
          </button>
        </div>
      </div>
      <dl className="nd-review-meta">
        <div>
          <dt>Variant</dt>
          <dd>{detail.variant_name ?? <span className="nd-muted">—</span>}</dd>
        </div>
        <div>
          <dt>Candidate</dt>
          <dd>{candidate}</dd>
        </div>
        <div>
          <dt>Pages</dt>
          <dd>
            {placed} of {placed + detail.missing_page_indices.length}
            {missing && <span className="nd-flag">⚠ {missing}</span>}
          </dd>
        </div>
        <div>
          <dt>{count.label}</dt>
          <dd>{count.value}</dd>
        </div>
        {detail.score && (
          <div>
            <dt>Score</dt>
            <dd>{formatScoreTriple(detail.score)}</dd>
          </div>
        )}
      </dl>
    </div>
  )
}

/**
 * The candidate identifier, and — while the lock is ours — its inline editor.
 * Enter saves, Escape cancels; an empty value clears the
 * candidate. The draft is local; the value shown outside edit mode is always
 * the cached review's, i.e. the server's.
 */
function CandidateField({
  detail, canEdit, editing, onEdit, onCancel, onSave, saving, inlineError,
}: {
  detail: SubmissionReviewDetail
  canEdit: boolean
  editing: boolean
  onEdit: () => void
  onCancel: () => void
  onSave: (value: string | null) => void
  saving: boolean
  /** A 422 for what was typed, shown under the input. */
  inlineError: string | null
}) {
  const [draft, setDraft] = useState('')
  // Prefilled from the stored value each time edit mode opens.
  useEffect(() => {
    if (editing) setDraft(detail.respondent_identifier ?? '')
    // Only on entering edit mode: a background refetch must not overwrite
    // what the operator is typing.
  }, [editing])

  function save() {
    if (saving) return
    const value = draft.trim()
    onSave(value === '' ? null : value)
  }

  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'Enter') { e.preventDefault(); save() }
    else if (e.key === 'Escape') { e.preventDefault(); onCancel() }
  }

  if (editing) {
    return (
      <div className="nd-candidate-edit">
        <div className="nd-candidate-edit-row">
          <input
            type="text"
            className="nd-candidate-edit-input"
            value={draft}
            maxLength={500}
            autoFocus
            aria-label="Candidate identifier"
            placeholder="No candidate"
            disabled={saving}
            onChange={e => setDraft(e.target.value)}
            onKeyDown={onKeyDown}
          />
          <button type="button" className="btn btn-primary btn-sm" disabled={saving} onClick={save}>
            Save
          </button>
          <button type="button" className="btn btn-secondary btn-sm" disabled={saving} onClick={onCancel}>
            Cancel
          </button>
        </div>
        {inlineError && <p className="error-msg nd-candidate-edit-error">{inlineError}</p>}
      </div>
    )
  }

  return (
    <>
      {detail.respondent_identifier ?? <span className="nd-muted">—</span>}
      {canEdit && (
        <>
          {' '}
          <button type="button" className="nd-link-btn" onClick={onEdit}>Edit</button>
        </>
      )}
      {showNoCandidate(detail) && <span className="nd-flag">⚠ No candidate</span>}
    </>
  )
}

/**
 * The lock situation, when it is not the normal one. Nothing while the
 * acquire is pending or the lock is ours: a banner saying "you may edit"
 * would be furniture.
 */
function LockBanner({
  view, lock, currentUserId, takingOver, onTakeOver,
}: {
  view: ReturnType<typeof lockView>
  lock: SubmissionLockInfo | null
  currentUserId: string | undefined
  takingOver: boolean
  onTakeOver: () => void
}) {
  if (view === 'closed') {
    return (
      <div className="nd-review-banner nd-review-banner-info" role="status">
        <span>This submission is closed. It is shown read-only.</span>
      </div>
    )
  }
  if (view !== 'held' && view !== 'lost') return null

  const name = lock ? lockHolderLabel(lock, currentUserId) : null
  const age = lock ? formatLockAge(lock.locked_at) : ''
  const held = view === 'held'
  return (
    <div
      className={`nd-review-banner ${held ? 'nd-review-banner-warning' : 'nd-review-banner-danger'}`}
      role="status"
    >
      {held ? (
        <span>
          <span aria-hidden="true">🔒 </span>
          {name
            ? <><b>In review by {name}</b>{age && `, ${age}`}.</>
            : <b>Someone else holds the review lock.</b>}{' '}
          You can look, but not make changes.
        </span>
      ) : (
        <span>
          <b>{name ?? 'Someone else'} took over this submission</b>{age && ` ${age}`}. Your view is now read-only.
        </span>
      )}
      <button type="button" className="btn btn-secondary btn-sm" disabled={takingOver} onClick={onTakeOver}>
        {held ? 'Take over' : 'Take back'}
      </button>
    </div>
  )
}
