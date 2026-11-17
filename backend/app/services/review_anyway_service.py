# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unread objects and "Review anyway".

A MATCHED submission stays MATCHED while any object of its bound sheets keeps
failing extraction (see "Writing SCANNED" in ``extraction_orchestrator_service``).
A torn corner, a coffee stain or a bad scan can make that permanent, and the
submission would never reach review. "Review anyway" is the explicit way out:
an operator brings it into review, with a PLACEHOLDER result for every object
no run could read, and then enters those answers from the paper sheet or flags
them.

Unread objects
--------------
The extractable objects (``extraction_orchestrator_service.is_extractable`` —
the very rule the extraction walk applies, not a copy of it) of a submission's
template-bound sheets that have no result for that (sheet, object) pair.
Deliberately NOT unread:

  * "not yet supported" objects (no extractor in this build): they never held a
    submission back, and will be read once their extractor exists;
  * never-evaluable objects (TEXT, IMAGE, a role-carrying CODE): not answers;
  * objects of a sheet matching could not place: there is no template to read
    them against.

:func:`unread_objects` is the ONE place that rule lives; the queue count and
the action both go through it.

Only after a run
----------------
"Review anyway" requires ``extraction_attempted_at``: a run must have examined
the submission at least once. Before that, every object is "unread" by
definition, and the action would turn a submission nobody tried to read into a
stack of manual entries. The queue applies the same condition, so the button is
never offered where the action would refuse.

Placeholders
------------
Typed exactly as the reading they stand in for
(``extraction_orchestrator_service.result_type_for``), with a NULL payload —
which is what marks them, since every extractor always writes one — a NULL
confidence, and ``validity_forces_review = True`` so they are in review whatever
the batch threshold, now and after any later change of it. A later run counts
them as already extracted and does not retry them.

During a run
------------
Refused with the structured 409 ``EXTRACTION_RUNNING`` while an extraction run
holds the submission's batch (``batch_operation_lock``): the run read at
its start which objects are unread, and a placeholder written meanwhile would
take a pair the run is about to read, failing the whole run. The operator tries
again once the run is over — when it may well have read those objects.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.enums import SubmissionStatus
from app.models.page_object import PageObject
from app.models.submission import Submission
from app.models.submission_object_result import SubmissionObjectResult
from app.models.submission_page import SubmissionPage
from app.models.user import User
from app.repositories import (
    page_object_repo,
    submission_batch_repo,
    submission_object_result_repo,
)
from app.schemas.submission import ReviewAnywayResult
from app.services import (
    audit_service,
    batch_operation_lock,
    extraction_orchestrator_service,
    submission_batch_service,
    submission_lock_service,
)

# Machine-readable code of the structured 409 "Review anyway" refuses with, and
# the values of its ``reason`` extra.
NOT_ELIGIBLE = "NOT_ELIGIBLE"
REASON_STATUS = "STATUS"
REASON_NOT_EXTRACTED = "NOT_EXTRACTED"

UnreadObjects = dict[uuid.UUID, list[tuple[SubmissionPage, PageObject]]]


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def unread_objects(
    pages: Iterable[SubmissionPage],
    objects_by_template_page: Mapping[uuid.UUID, list[PageObject]],
    existing_pairs: set[tuple[uuid.UUID, uuid.UUID]],
) -> UnreadObjects:
    """{submission_id: [(sheet, object), ...]} of the objects a run could read
    and has no result for (see the module docstring).

    Pure: the caller loads the data — the sheets, the objects of their template
    pages (with role) and the existing (sheet, object) result pairs — so one
    submission and a whole batch cost the same handful of queries. A
    submission with nothing unread is absent. Unbound sheets are skipped.
    """
    unread: UnreadObjects = {}
    for page in pages:
        if page.form_variant_page_id is None:
            continue
        for page_object in objects_by_template_page.get(page.form_variant_page_id, []):
            if (page.id, page_object.id) in existing_pairs:
                continue
            if not extraction_orchestrator_service.is_extractable(page_object):
                continue
            unread.setdefault(page.submission_id, []).append((page, page_object))
    return unread


def _objects_by_template_page(
    db: Session, pages: list[SubmissionPage]
) -> dict[uuid.UUID, list[PageObject]]:
    template_page_ids = {p.form_variant_page_id for p in pages if p.form_variant_page_id}
    grouped: dict[uuid.UUID, list[PageObject]] = {}
    for obj in page_object_repo.list_for_template_pages_with_role(db, template_page_ids):
        grouped.setdefault(obj.form_variant_page_id, []).append(obj)
    return grouped


def unread_objects_for_submission(
    db: Session, submission: Submission
) -> list[tuple[SubmissionPage, PageObject]]:
    """The unread (sheet, object) pairs of ONE submission, in sheet order.

    Three queries — its bound sheets, their template pages' objects, and the
    result pairs on those sheets — scoped to the submission rather than its
    batch, since one submission must not cost a whole batch's results.
    """
    pages = submission_batch_repo.list_bound_pages_for_submissions(db, [submission.id])
    existing = submission_object_result_repo.existing_object_pairs_for_pages(
        db, [p.id for p in pages]
    )
    return unread_objects(pages, _objects_by_template_page(db, pages), existing).get(
        submission.id, []
    )


def _is_candidate(submission: Submission) -> bool:
    # Where "Review anyway" could apply: MATCHED, and examined by a run.
    return (
        submission.status is SubmissionStatus.MATCHED
        and submission.extraction_attempted_at is not None
    )


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------


def unread_counts_for_queue(
    db: Session, batch_id: uuid.UUID, submissions: Iterable[Submission]
) -> dict[uuid.UUID, int]:
    """{submission_id: unread objects} for the listed submissions of a batch.

    Counted ONLY where "Review anyway" could apply — MATCHED and examined by a
    run — so the number means "what the button would turn into placeholders".
    Every other submission is absent (read as 0). A CONSTANT number of queries
    however many submissions are listed: none when no submission qualifies,
    otherwise three — their bound sheets, the objects of those sheets' template
    pages, and the batch's existing result pairs.
    """
    candidate_ids = [s.id for s in submissions if _is_candidate(s)]
    if not candidate_ids:
        return {}
    pages = submission_batch_repo.list_bound_pages_for_submissions(db, candidate_ids)
    objects = _objects_by_template_page(db, pages)
    existing = submission_object_result_repo.existing_object_pairs_for_batch(db, batch_id)
    return {
        submission_id: len(found)
        for submission_id, found in unread_objects(pages, objects, existing).items()
    }


# ---------------------------------------------------------------------------
# The action
# ---------------------------------------------------------------------------


def _placeholder(page: SubmissionPage, page_object: PageObject) -> SubmissionObjectResult:
    return SubmissionObjectResult(
        submission_page_id=page.id,
        page_object_id=page_object.id,
        result_type=extraction_orchestrator_service.result_type_for(page_object),
        # NULL payload: what marks a placeholder (see the model).
        detected_payload_json=None,
        confidence_score=None,
        # On the axis, so a threshold change can never clear it.
        validity_forces_review=True,
        review_required=True,
    )


def review_anyway(
    db: Session, submission_id: uuid.UUID, user: User
) -> ReviewAnywayResult:
    """Bring a MATCHED submission into review despite objects no run could
    read.

    Any project member, no ACTIVE gate (review continues after collection has
    closed). Once the batch is taken, the submission is re-read ``FOR UPDATE``,
    so two operators pressing the button at once serialise on it. Refused with
    a structured 409 ``EXTRACTION_RUNNING`` while a run holds its batch (see
    "During a run") — first, so during a run even a submission that is not
    eligible gets that one — then ``NOT_ELIGIBLE`` when it is not MATCHED
    (``reason: STATUS``) or no run has examined it yet
    (``reason: NOT_EXTRACTED``).

    Creates a placeholder for every unread object, moves the submission to
    SCANNED — also with 0 unread objects, when a run read everything in the
    meantime — and records ``submission.reviewed_anyway``. Should a run's
    reading of one of the same pairs still surface as the uniqueness violation
    (the batch lock leaves no known way for it to), it is refused as
    ``SUBMISSION_CHANGED`` and nothing is written, so the operator reloads.

    Owns the transaction.
    """
    # Authorised first, without locking the row: a run holds the rows of the
    # submissions it has walked until it commits, so taking this one before the
    # batch would wait out the run instead of being refused.
    submission, _project = submission_batch_service.load_submission_authorized(
        db, submission_id, user
    )
    # Not while a run holds the batch (see "During a run"); then no run holds
    # any of its rows, and the row lock below only serialises two operators.
    batch_operation_lock.acquire_shared(db, submission.submission_batch_id)
    submission = submission_batch_repo.get_submission_for_update(db, submission_id)
    if submission.status is not SubmissionStatus.MATCHED:
        raise submission_lock_service.structured_conflict(
            NOT_ELIGIBLE,
            f"Only a MATCHED submission can be reviewed anyway; this one is "
            f"{submission.status.value}.",
            reason=REASON_STATUS,
            status=submission.status.value,
        )
    if submission.extraction_attempted_at is None:
        raise submission_lock_service.structured_conflict(
            NOT_ELIGIBLE,
            "Run extraction at least once before reviewing anyway.",
            reason=REASON_NOT_EXTRACTED,
        )

    unread = unread_objects_for_submission(db, submission)
    for page, page_object in unread:
        submission_object_result_repo.add(db, _placeholder(page, page_object))
    submission.status = SubmissionStatus.SCANNED
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise submission_lock_service.structured_conflict(
            submission_lock_service.SUBMISSION_CHANGED,
            "An extraction run read this submission in the meantime. Reload it.",
        )

    audit_service.log_event(
        db,
        actor_user_id=user.id,
        action=audit_service.SUBMISSION_REVIEWED_ANYWAY,
        entity_type="Submission",
        entity_id=submission.id,
        metadata={
            "placeholders_created": len(unread),
            "page_object_ids": [str(obj.id) for _page, obj in unread],
        },
    )
    db.commit()
    return ReviewAnywayResult(status=submission.status, placeholders_created=len(unread))
