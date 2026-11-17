# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Closing a submission and reopening it.

An operator ends the review of a submission with an explicit act, "Close
submission": SCANNED → REVIEWED, the soft lock released, the submission
read-only from then on — until it is reopened (see "Reopening"). Always
available on a SCANNED submission, however many readings are still
unresolved: those are NOT written to, they simply stay ``review_outcome IS
NULL`` on a closed submission, and that IS what "unresolved" means, told apart
in scoring and export from the candidate's NO_ANSWER and from a FLAG. A
"Review anyway" placeholder nobody reviewed is one of them.

A review save
-------------
Closing goes through ``submission_lock_service.guard_review_save`` like every
other review write, so it is refused in the same three shapes: not SCANNED
(already closed included) → ``NOT_REVIEWABLE`` with ``status``; actively locked
by a colleague → ``LOCK_LOST``; built on a stale ``review_version`` →
``SUBMISSION_CHANGED``. The version check is what makes the FE's confirmation
honest: the operator confirms "N unresolved" on the version they see, and the
server closes that version or nothing — no confirmation flag in the request.
The bump of the version also tells a colleague's stale screen that the
submission moved.

During a run
------------
Refused with ``EXTRACTION_RUNNING`` while an extraction run holds the batch: the
batch is taken SHARED (``batch_operation_lock``) BEFORE the submission's row,
as "Review anyway" does, so a close never waits out a run on a row the run
holds. The run already skips a submission closed under it; refusing the close
during the run also removes the residual case where a submission closed while
the run was reading it got that run's readings.

The score
---------
Closing writes ``total_score``: the raw score computed at that moment by
``submission_score_service``, the very computation the review page shows, so
the value saved is the last one the operator saw. Only the raw score: the
maximum is fixed per variant and the percentage follows from it. Not in the
audit either — no history of scores is kept. When the score cannot be
computed (a broken invariant, logged by the service) the close still
succeeds and ``total_score`` stays NULL: a bug in the data must not stop the
operator, who cannot fix it.

Who and when
------------
Only in the audit trail, ``submission.closed`` with ``{unresolved_count,
review_version}`` — no column on the Submission.

Reopening
---------
"Reopen submission" undoes a close: REVIEWED → SCANNED, by any project
member, with no confirmation. Every review and every ruling stays as it is;
the reopener gets the lock, ``review_version`` is bumped, and ``total_score``
is cleared: reopening invalidates the score, which the next close writes
again (see "The score").

Not a review save: ``guard_review_save`` admits SCANNED only, and a closed
submission has no lock to lose (closing clears it, ``acquire`` refuses one).
So the reopen checks for itself, on the row read ``FOR UPDATE``: anything but
REVIEWED — FINALIZED (no path reaches it today), UNRESOLVABLE (it has its own
Undo), SCANNED because a colleague reopened first — → ``INVALID_STATUS`` with
``status``, the code of a transition from the wrong status, whose message for
the SCANNED case says just that; a stale version → ``SUBMISSION_CHANGED``, so
an operator reopens only what they saw.

While it was closed, threshold changes left its unresolved results alone.
Reopened, it is open like any other, so those results are recomputed once
against the threshold now in force; reviewed ones are spared as always. The
batch row is read ``FOR UPDATE`` first, after the shared batch lock and before
the submission's row: a threshold change writes that row before reading the
results, so the two serialise and neither misses the other. The candidate is
not touched: the rule that lets a reading set it wants a submission nobody
has saved on (``review_version`` 0), and a closed one has been through at
least the close — a threshold change would not set it either.

Refused with ``EXTRACTION_RUNNING`` during a run, like the close. Recorded as
``submission.reopened`` with ``{unresolved_count, review_version}``.

The response is the whole review page, re-read after the commit, like every
review action. Constant statements, whatever the submission holds.
"""
from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.models.enums import SubmissionStatus
from app.models.user import User
from app.repositories import submission_batch_repo, submission_object_result_repo
from app.schemas.submission import SubmissionReviewDetail
from app.services import (
    audit_service,
    batch_operation_lock,
    confidence_threshold_service,
    submission_batch_service,
    submission_lock_service,
    submission_review_service,
    submission_score_service,
)


def close_submission(
    db: Session, submission_id: uuid.UUID, user: User, declared_version: int
) -> SubmissionReviewDetail:
    """Close a SCANNED submission: REVIEWED, score written, lock released,
    audited with how many readings were left unresolved. Owns the
    transaction."""
    # Authorised first, without locking the row: the batch must be taken
    # before the row (see "During a run").
    submission, _project = submission_batch_service.load_submission_authorized(
        db, submission_id, user
    )
    batch_operation_lock.acquire_shared(db, submission.submission_batch_id)
    submission = submission_lock_service.guard_review_save(
        db, submission_id, user, declared_version
    )
    unresolved = submission_object_result_repo.count_pending_review_for_submission(
        db, submission.id
    )
    # Computed while still SCANNED, on the results as they are: the score the
    # review page was showing. None (logged) on a broken invariant.
    score = submission_score_service.compute(db, submission)
    submission.total_score = score.raw_score if score is not None else None
    submission.status = SubmissionStatus.REVIEWED
    # The guard has just taken the lock for the caller; closing lets it go.
    submission.locked_by = None
    submission.locked_at = None
    audit_service.log_event(
        db,
        actor_user_id=user.id,
        action=audit_service.SUBMISSION_CLOSED,
        entity_type="Submission",
        entity_id=submission.id,
        metadata={
            "unresolved_count": unresolved,
            "review_version": submission.review_version,
        },
    )
    db.commit()
    return submission_review_service.get_review_detail(db, submission_id, user)


def reopen_submission(
    db: Session, submission_id: uuid.UUID, user: User, declared_version: int
) -> SubmissionReviewDetail:
    """Reopen a REVIEWED submission: SCANNED, locked to *user*, version bumped,
    score cleared, unresolved results caught up with the threshold in force;
    audited (module docstring, "Reopening"). Owns the transaction."""
    submission, _project = submission_batch_service.load_submission_authorized(
        db, submission_id, user
    )
    batch_operation_lock.acquire_shared(db, submission.submission_batch_id)
    batch = submission_batch_repo.get_for_update(db, submission.submission_batch_id)
    if batch is None:
        # Not reachable: the foreign key guarantees the batch. Said plainly
        # rather than left to fail inside the threshold lookup.
        raise RuntimeError(f"Submission {submission_id} has no batch")
    submission, _project = submission_batch_service.load_submission_authorized(
        db, submission_id, user, for_update=True
    )
    if submission.status is not SubmissionStatus.REVIEWED:
        raise submission_lock_service.structured_conflict(
            submission_lock_service.INVALID_STATUS,
            _not_reopenable_message(submission.status),
            status=submission.status.value,
        )
    if declared_version != submission.review_version:
        raise submission_lock_service.structured_conflict(
            submission_lock_service.SUBMISSION_CHANGED,
            "This submission has changed since you opened it. Reload it before reopening.",
            current_version=submission.review_version,
        )
    submission.status = SubmissionStatus.SCANNED
    submission_lock_service.take(submission, user)
    submission.review_version += 1
    submission.total_score = None
    confidence_threshold_service.recompute_review_required_for_submission(
        db, batch, submission.id
    )
    db.flush()
    unresolved = submission_object_result_repo.count_pending_review_for_submission(
        db, submission.id
    )
    audit_service.log_event(
        db,
        actor_user_id=user.id,
        action=audit_service.SUBMISSION_REOPENED,
        entity_type="Submission",
        entity_id=submission.id,
        metadata={
            "unresolved_count": unresolved,
            "review_version": submission.review_version,
        },
    )
    db.commit()
    return submission_review_service.get_review_detail(db, submission_id, user)


def _not_reopenable_message(status: SubmissionStatus) -> str:
    """The ``INVALID_STATUS`` message of a refused reopen. SCANNED is the usual
    case — a colleague reopened it first — and gets words of its own."""
    if status is SubmissionStatus.SCANNED:
        return "This submission has already been reopened."
    return f"Only a closed submission can be reopened; this one is {status.value}."
