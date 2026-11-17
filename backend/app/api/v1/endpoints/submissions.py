# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Endpoints acting on ONE submission.

Router:
  - router  (mounted at /submissions)
      GET    /{submission_id}/review
      POST   /{submission_id}/assign-variant
      POST   /{submission_id}/mark-unresolvable
      POST   /{submission_id}/undo-unresolvable
      POST   /{submission_id}/review-anyway
      PATCH  /{submission_id}/candidate
      POST   /{submission_id}/results/{result_id}/review
      POST   /{submission_id}/accept-remaining
      POST   /{submission_id}/close
      POST   /{submission_id}/reopen
      POST   /{submission_id}/lock
      POST   /{submission_id}/lock/take-over
      DELETE /{submission_id}/lock

Its own module and its own prefix because the resource is a Submission, not a
SubmissionBatch: the batch is how a submission is created and listed, but
manual recovery addresses a single submission directly and needs no batch in
the path. The service logic stays in ``submission_batch_service`` — same
domain, same authorisation helpers — exactly as ``page_objects`` keeps
object-scoped routes beside the page-scoped ones it belongs with. The review
soft lock is the exception: its rules are their own module,
``submission_lock_service``, and so is the review page (the read and the
candidate edit), ``submission_review_service``, "Review anyway",
``review_anyway_service``, the review actions, ``review_action_service``, and
closing the submission and reopening it, ``submission_close_service``.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.deps import get_db, require_user
from app.models.user import User
from app.schemas.submission import (
    ReviewAnywayResult,
    SubmissionAcceptRemainingRequest,
    SubmissionAcceptRemainingResult,
    SubmissionCandidateState,
    SubmissionCandidateUpdate,
    SubmissionCloseRequest,
    SubmissionLockState,
    SubmissionRead,
    SubmissionReopenRequest,
    SubmissionReviewActionRequest,
    SubmissionReviewDetail,
    SubmissionVariantAssign,
)
from app.services import (
    review_action_service,
    review_anyway_service,
    submission_batch_service,
    submission_close_service,
    submission_lock_service,
    submission_review_service,
)

router = APIRouter()


@router.get(
    "/{submission_id}/review",
    response_model=SubmissionReviewDetail,
    status_code=status.HTTP_200_OK,
)
def get_submission_review(
    submission_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Everything the submission review page shows for one submission.

    The header (variant, candidate, status, missing pages, active lock,
    ``review_version``), every physical sheet in order with its template page
    and size, and on each sheet the readings that needed a human — resolved or
    not — with why they were flagged.

    A pure read: it does NOT take or renew the review lock (the FE calls
    ``POST /lock`` itself) and records nothing. Allowed in every status, so a
    closed submission can be opened read-only. Any project member may call it,
    and the project need NOT be ACTIVE.
    """
    return submission_review_service.get_review_detail(db, submission_id, user)


@router.post(
    "/{submission_id}/assign-variant",
    response_model=SubmissionRead,
    status_code=status.HTTP_200_OK,
)
def assign_submission_variant(
    submission_id: uuid.UUID,
    data: SubmissionVariantAssign,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Name the FormVariant of a submission the matching engine could not resolve.

    For MATCH_FAILED submissions only (409 otherwise, the current status named
    in the message). The variant must be a PUBLISHED variant of the submission's
    own project — 404 if it is neither, 409 if it is not published.

    Assigning moves the submission to MATCHED and then runs the ordinary engine
    over it, so its pages are bound and scored by the SAME Level 2 rules an
    automatic match uses. Recorded as ``submission.manually_matched``, distinct
    from the automatic ``submission.matched``.

    Any project member may do it, and the project need NOT be ACTIVE: like the
    matching re-run, recovery must stay possible after collection has closed.
    """
    submission = submission_batch_service.assign_variant_manually(
        db, submission_id, data.form_variant_id, user
    )
    return SubmissionRead.model_validate(submission)


@router.post(
    "/{submission_id}/mark-unresolvable",
    response_model=SubmissionRead,
    status_code=status.HTTP_200_OK,
)
def mark_submission_unresolvable(
    submission_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Close a submission nobody can sort out.

    MATCH_FAILED or UNASSIGNED → UNRESOLVABLE: closed, never finalized, not
    blocking the batch's export, and left alone by matching; the status it came
    from is kept in ``unresolvable_from_status``. From those two statuses only,
    else the structured 409 ``INVALID_STATUS`` with the ``status`` found. Any
    project member, the project need NOT be ACTIVE. Recorded as
    ``submission.marked_unresolvable``.
    """
    submission = submission_batch_service.mark_unresolvable(db, submission_id, user)
    return SubmissionRead.model_validate(submission)


@router.post(
    "/{submission_id}/undo-unresolvable",
    response_model=SubmissionRead,
    status_code=status.HTTP_200_OK,
)
def undo_submission_unresolvable(
    submission_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Take an UNRESOLVABLE submission back to the status it was marked
    from, MATCH_FAILED or UNASSIGNED.

    From UNRESOLVABLE only, else the structured 409 ``INVALID_STATUS``. Same
    access rules as marking it. Recorded as ``submission.unresolvable_undone``.
    """
    submission = submission_batch_service.undo_unresolvable(db, submission_id, user)
    return SubmissionRead.model_validate(submission)


# ===========================================================================
# Review soft lock
# ===========================================================================


@router.post(
    "/{submission_id}/lock",
    response_model=SubmissionLockState,
    status_code=status.HTTP_200_OK,
)
def acquire_submission_lock(
    submission_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Take the review lock of a submission on opening it — or learn who has it.

    Called on open and as the periodic renewal: free, expired or already the
    caller's, the lock becomes (stays) the caller's and ``acquired`` is true.
    If a colleague is actively reviewing it, nothing changes: ``acquired`` is
    false and ``lock`` names them, for "In review by X" and a Take over button.
    That is a 200, not an error — finding a colleague there is a normal outcome.

    409 ``NOT_REVIEWABLE`` (structured ``detail``) unless the submission is
    SCANNED. Any project member may call it, and the project need NOT be
    ACTIVE: review goes on after collection has closed.
    """
    return submission_lock_service.acquire(db, submission_id, user)


@router.post(
    "/{submission_id}/lock/take-over",
    response_model=SubmissionLockState,
    status_code=status.HTTP_200_OK,
)
def take_over_submission_lock(
    submission_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Take the review lock of a submission away from whoever holds it.

    Always acquires; the previous holder's next save is then refused with
    ``LOCK_LOST``. This is also the only "unlock" there is — no admin route
    exists because any member can do this. Taking an ACTIVE lock from a
    colleague is recorded as ``submission.lock_taken_over``.

    409 ``NOT_REVIEWABLE`` unless SCANNED. Any project member, no ACTIVE gate.
    """
    return submission_lock_service.take_over(db, submission_id, user)


@router.delete("/{submission_id}/lock", status_code=status.HTTP_204_NO_CONTENT)
def release_submission_lock(
    submission_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Release the caller's review lock on leaving the submission.

    Best-effort and idempotent: a lock that is not the caller's (taken over,
    expired, or never there) is left untouched and the call still succeeds, so
    a late or repeated exit can never undo a colleague's lock. Allowed in any
    status. Any project member, no ACTIVE gate.
    """
    submission_lock_service.release(db, submission_id, user)


# ===========================================================================
# "Review anyway" and the manual candidate edit
# ===========================================================================


@router.post(
    "/{submission_id}/review-anyway",
    response_model=ReviewAnywayResult,
    status_code=status.HTTP_200_OK,
)
def review_submission_anyway(
    submission_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Bring a MATCHED submission into review despite objects no run could read.

    Creates a placeholder result (no reading; "Could not be read" in review)
    for every object an extraction run could read but has no result for, then
    moves the submission to SCANNED. Recorded as ``submission.reviewed_anyway``.

    409 ``NOT_ELIGIBLE`` (structured ``detail``, with ``reason``) unless the
    submission is MATCHED (``STATUS``) and a run has examined it at least once
    (``NOT_EXTRACTED``); 409 ``EXTRACTION_RUNNING`` while an extraction run is
    in progress on its batch; 409 ``SUBMISSION_CHANGED`` if a run's
    reading of one of the same results still surfaces. Any project member, no
    ACTIVE gate.
    """
    return review_anyway_service.review_anyway(db, submission_id, user)


@router.patch(
    "/{submission_id}/candidate",
    response_model=SubmissionCandidateState,
    status_code=status.HTTP_200_OK,
)
def edit_submission_candidate(
    submission_id: uuid.UUID,
    data: SubmissionCandidateUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Set, correct or clear a submission's candidate identifier by hand.

    A review save: the value is trimmed (blank clears it, over 500 characters
    is a 422), and the call is admitted by the review-save guard — 409
    ``NOT_REVIEWABLE`` unless SCANNED, ``LOCK_LOST`` while a colleague holds
    the lock, ``SUBMISSION_CHANGED`` when ``review_version`` is stale. Returns
    the new value, the version to declare next and the caller's lock, so no
    refetch is needed. Recorded as ``submission.candidate_edited``.
    """
    return submission_review_service.edit_candidate(
        db, submission_id, user, data.respondent_identifier, data.review_version
    )


# ===========================================================================
# Review actions and "Accept all remaining"
# ===========================================================================


@router.post(
    "/{submission_id}/results/{result_id}/review",
    response_model=SubmissionReviewDetail,
    status_code=status.HTTP_200_OK,
)
def review_submission_result(
    submission_id: uuid.UUID,
    result_id: uuid.UUID,
    data: SubmissionReviewActionRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Rule on one reading of a submission: CONFIRM, SUBSTITUTE, FLAG or UNDO,
    with an optional note (trimmed, at most 2000 characters).

    A review save: 404 when the result is not one of this submission's, then
    the review-save guard (409 ``NOT_REVIEWABLE`` / ``LOCK_LOST`` /
    ``SUBMISSION_CHANGED``), then the action's own rules — 409
    ``NOT_CONFIRMABLE`` (with ``reason``) for a CONFIRM of an incomplete
    reading, 422 for a SUBSTITUTE without a valid ``value``, 409
    ``NOT_RESOLVED`` for an UNDO of an unresolved reading. Confirming or
    substituting a candidate identifier also sets the submission's candidate.
    Returns the whole review page, re-read after the save.
    """
    return review_action_service.apply_action(
        db, submission_id, result_id, user, data
    )


@router.post(
    "/{submission_id}/accept-remaining",
    response_model=SubmissionAcceptRemainingResult,
    status_code=status.HTTP_200_OK,
)
def accept_remaining_submission_results(
    submission_id: uuid.UUID,
    data: SubmissionAcceptRemainingRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Confirm, in one save, every unresolved reading of the submission that
    may be accepted unseen — not free text, not an ambiguous or unreadable
    respondent ID, not an incomplete reading (``bulk_acceptable`` on the review
    page). The review-save guard applies, and the version is bumped once even
    when nothing was eligible (``accepted_count`` 0). Recorded as
    ``submission.accepted_remaining``. Returns the whole review page plus
    ``accepted_count``.
    """
    return review_action_service.accept_remaining(
        db, submission_id, user, data.review_version
    )


# ===========================================================================
# Closing the submission
# ===========================================================================


@router.post(
    "/{submission_id}/close",
    response_model=SubmissionReviewDetail,
    status_code=status.HTTP_200_OK,
)
def close_submission(
    submission_id: uuid.UUID,
    data: SubmissionCloseRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Close a SCANNED submission: status REVIEWED, ``total_score`` written with
    the raw score of that moment (null if it cannot be computed), review lock
    released, read-only until reopened. Allowed however many readings are still
    unresolved — they stay unresolved and are counted as unresolved at
    closing; the confirmation is the FE's, on the ``review_version`` sent
    here.

    A review save: 409 ``EXTRACTION_RUNNING`` while a run holds the batch, then
    the review-save guard (``NOT_REVIEWABLE`` with ``status`` — also when already
    closed —, ``LOCK_LOST``, ``SUBMISSION_CHANGED``). Recorded as
    ``submission.closed`` with ``unresolved_count``. Returns the whole review
    page, re-read after the close.
    """
    return submission_close_service.close_submission(
        db, submission_id, user, data.review_version
    )


# ===========================================================================
# Reopening the submission
# ===========================================================================


@router.post(
    "/{submission_id}/reopen",
    response_model=SubmissionReviewDetail,
    status_code=status.HTTP_200_OK,
)
def reopen_submission(
    submission_id: uuid.UUID,
    data: SubmissionReopenRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Reopen a REVIEWED submission: status SCANNED, the review lock the
    caller's, every review kept, the score cleared, its unresolved readings
    recomputed with the batch's current threshold. Any project member, no
    ACTIVE gate, no confirmation.

    409 ``EXTRACTION_RUNNING`` while a run holds the batch; ``INVALID_STATUS``
    with ``status`` unless REVIEWED (a colleague reopening first included);
    ``SUBMISSION_CHANGED`` on a stale ``review_version``. Recorded as
    ``submission.reopened``. Returns the whole review page, re-read after the
    reopen.
    """
    return submission_close_service.reopen_submission(
        db, submission_id, user, data.review_version
    )
