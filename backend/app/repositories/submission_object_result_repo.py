# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Data-access layer for SubmissionObjectResult.

Scoped to what the confidence-threshold recomputation, the extractor that
writes these rows, the per-submission pending-review count, its per-batch
counterpart on the batch list, the submission review page, "Review anyway",
the review actions and the candidate set from a respondent-ID reading need,
plus the results the submission score and the Detail export read. Scoring a
result is ``scoring_rules``' business, not this module's.

"Resolved" — a human has ruled on the result — is ``review_outcome IS NOT
NULL`` everywhere in this module. "A ``submission_reviews`` row exists" is
not the test: that log is append-only and records UNDO too, so an undone
result has rows and is NOT resolved. The ``reviewed`` boolean is not it
either: nothing writes that column.

A result hangs off a ``SubmissionPage``, which hangs off a ``Submission``, which
carries the ``submission_batch_id``; so every batch-wide query here goes through
that two-hop join. Kept in the repo rather than inlined in the service for the
same reason ``submission_batch_repo`` exists: the join shape is data access, and
the service should read as the rule it applies.
"""
from __future__ import annotations

import uuid
from collections.abc import Collection

from datetime import datetime
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.orm import Session, joinedload

from app.models.enums import (
    CLOSED_SUBMISSION_STATUSES,
    ReviewAction,
    ReviewOutcome,
    SubmissionResultType,
    SubmissionStatus,
)
from app.models.page_object import PageObject
from app.models.question_object_config import QuestionObjectConfig
from app.models.submission import Submission
from app.models.submission_batch import SubmissionBatch
from app.models.submission_object_result import SubmissionObjectResult
from app.models.submission_page import SubmissionPage
from app.models.submission_review import SubmissionReview


def add(db: Session, result: SubmissionObjectResult) -> SubmissionObjectResult:
    """Stage *result* for insertion and return it.

    Neither flushes nor commits — the caller owns the transaction, the same
    discipline the whole scanning pipeline follows (see
    ``submission_matching_service``), so extracting a page's objects and
    whatever else that page's processing entails land as one act.

    That deferral means the ``(submission_page_id, page_object_id)`` uniqueness
    is NOT reported here: a duplicate reading surfaces as an
    ``IntegrityError`` at the caller's flush/commit, not at this call.
    """
    db.add(result)
    return result


def existing_object_pairs_for_batch(
    db: Session, batch_id: uuid.UUID
) -> set[tuple[uuid.UUID, uuid.UUID]]:
    """``{(submission_page_id, page_object_id)}`` already extracted in *batch_id*.

    The whole batch's answer in ONE query, deliberately. The extraction
    orchestrator has to know, for every authored object on every sheet, whether
    a result already exists — and asking that per object would be a query per
    object: hundreds of submissions times dozens of objects each. It is the same
    reasoning as ``submission_batch_repo.page_counts``.

    A set of PAIRS rather than of ids, because that pair is exactly the
    uniqueness the table enforces. Returns only what is already
    VISIBLE to this session's transaction: results the caller stages during its
    own run are its business to remember.

    Two-hop join to the batch, like :func:`list_unreviewed_for_batch`.
    """
    stmt = (
        select(
            SubmissionObjectResult.submission_page_id,
            SubmissionObjectResult.page_object_id,
        )
        .join(
            SubmissionPage,
            SubmissionPage.id == SubmissionObjectResult.submission_page_id,
        )
        .join(Submission, Submission.id == SubmissionPage.submission_id)
        .where(Submission.submission_batch_id == batch_id)
    )
    return {(page_id, object_id) for page_id, object_id in db.execute(stmt).all()}


def list_unreviewed_for_batch(
    db: Session, batch_id: uuid.UUID
) -> list[SubmissionObjectResult]:
    """Every result of *batch_id* that is not resolved: ``review_outcome IS
    NULL`` (see the module docstring), in a submission still OPEN.

    This is the set the threshold recomputation is allowed to touch: a result a
    human has ruled on keeps the ``review_required`` it had at review time,
    whatever the threshold becomes afterwards. That is reversible: an UNDO
    sets ``review_outcome`` back to NULL, and the result is recomputed again
    on the next threshold change, exactly as one never reviewed.

    A CLOSED submission (``CLOSED_SUBMISSION_STATUSES``) is left out whole: a
    human closed it, and its unresolved results are frozen at closing like
    every ruling — a later threshold change must neither turn one into an
    automatic reading nor change the count left at closing. Reopening brings
    it back into the set, and catches it up with the threshold in force
    (:func:`list_unreviewed_for_submission`).

    Returns ORM objects rather than issuing a bulk UPDATE: the caller decides
    per row, and objects the caller's session already holds must not silently
    go stale behind it. A batch holds thousands of results, not millions.
    """
    stmt = (
        select(SubmissionObjectResult)
        .join(
            SubmissionPage,
            SubmissionPage.id == SubmissionObjectResult.submission_page_id,
        )
        .join(Submission, Submission.id == SubmissionPage.submission_id)
        .where(
            Submission.submission_batch_id == batch_id,
            Submission.status.not_in(CLOSED_SUBMISSION_STATUSES),
            SubmissionObjectResult.review_outcome.is_(None),
        )
    )
    return list(db.scalars(stmt).all())


def list_unreviewed_for_submission(
    db: Session, submission_id: uuid.UUID
) -> list[SubmissionObjectResult]:
    """Every result of one submission that is not resolved (``review_outcome IS
    NULL``) — :func:`list_unreviewed_for_batch` narrowed to *submission_id*.

    For reopening: while the submission was closed no
    threshold change reached these results, so on reopening they are
    recomputed once against the threshold now in force. No status filter: the
    caller is the reopen, which has just made the submission open again.
    """
    stmt = (
        select(SubmissionObjectResult)
        .join(
            SubmissionPage,
            SubmissionPage.id == SubmissionObjectResult.submission_page_id,
        )
        .where(
            SubmissionPage.submission_id == submission_id,
            SubmissionObjectResult.review_outcome.is_(None),
        )
    )
    return list(db.scalars(stmt).all())


def pending_review_counts_for_batch(
    db: Session, batch_id: uuid.UUID
) -> dict[uuid.UUID, int]:
    """{submission_id: results still awaiting review} for one batch, in ONE
    grouped query.

    "Awaiting review" is ``review_required`` AND not resolved
    (``review_outcome IS NULL``, the same test as
    :func:`list_unreviewed_for_batch`): an UNDONE result is pending again. Batched
    like ``submission_batch_repo.page_counts``, so the batch detail does not
    turn into a query per submission.

    Computed on every call and never stored: ``review_required`` is re-derived
    whenever the batch threshold moves, so a live count follows it
    for free where a persisted one would have to be kept in step.

    A submission with nothing pending is simply ABSENT from the dict — including
    one that has no results at all (UNASSIGNED, MATCH_FAILED, never extracted),
    which the inner joins drop without any explicit exclusion. The caller reads
    it with ``.get(submission_id, 0)``.

    Counted in EVERY status, closed submissions included: on a REVIEWED one
    these are the results left unresolved at closing, and the queue shows them
    as such, apart from the work still to do. The per-batch count below leaves the
    closed ones out instead: there it measures work left.
    """
    stmt = (
        select(SubmissionPage.submission_id, func.count(SubmissionObjectResult.id))
        # Anchored explicitly: the first selected column belongs to
        # submission_pages, which would otherwise become the left side of the
        # join below and make it a self-join.
        .select_from(SubmissionObjectResult)
        .join(
            SubmissionPage,
            SubmissionPage.id == SubmissionObjectResult.submission_page_id,
        )
        .join(Submission, Submission.id == SubmissionPage.submission_id)
        .where(
            Submission.submission_batch_id == batch_id,
            SubmissionObjectResult.review_required.is_(True),
            SubmissionObjectResult.review_outcome.is_(None),
        )
        .group_by(SubmissionPage.submission_id)
    )
    return {submission_id: count for submission_id, count in db.execute(stmt).all()}


def count_pending_review_for_submission(
    db: Session, submission_id: uuid.UUID
) -> int:
    """How many results of ONE submission still await review — the definition
    of :func:`pending_review_counts_for_batch` (``review_required`` AND
    ``review_outcome IS NULL``), for one submission, in one statement.

    Closing a submission records it: at that moment they become the
    results left unresolved at closing.
    """
    stmt = (
        select(func.count(SubmissionObjectResult.id))
        .join(
            SubmissionPage,
            SubmissionPage.id == SubmissionObjectResult.submission_page_id,
        )
        .where(
            SubmissionPage.submission_id == submission_id,
            SubmissionObjectResult.review_required.is_(True),
            SubmissionObjectResult.review_outcome.is_(None),
        )
    )
    return db.scalar(stmt) or 0


def pending_review_counts_by_batch_for_project(
    db: Session,
    project_id: uuid.UUID,
    excluded_statuses: Collection[SubmissionStatus],
) -> dict[uuid.UUID, int]:
    """{batch_id: results still awaiting review} for every batch of a project,
    in ONE grouped query.

    "Awaiting review" is exactly the definition of
    :func:`pending_review_counts_for_batch` — ``review_required`` AND
    ``review_outcome IS NULL`` — only grouped one level up, by batch, and
    joined one level further, to the project, so the batch list costs one query
    however many batches it shows.

    Results of submissions whose status is in *excluded_statuses* are left out.
    WHICH statuses those are is the caller's rule (the service passes the
    closed ones), not this function's: the repo only counts.

    A batch with nothing pending is simply ABSENT; the caller reads it with
    ``.get(batch_id, 0)``.
    """
    stmt = (
        select(Submission.submission_batch_id, func.count(SubmissionObjectResult.id))
        # Anchored explicitly, as in pending_review_counts_for_batch: the first
        # selected column belongs to submissions, which would otherwise become
        # the left side of the joins below and make one of them a self-join.
        .select_from(SubmissionObjectResult)
        .join(
            SubmissionPage,
            SubmissionPage.id == SubmissionObjectResult.submission_page_id,
        )
        .join(Submission, Submission.id == SubmissionPage.submission_id)
        .join(SubmissionBatch, SubmissionBatch.id == Submission.submission_batch_id)
        .where(
            SubmissionBatch.form_project_id == project_id,
            Submission.status.not_in(tuple(excluded_statuses)),
            SubmissionObjectResult.review_required.is_(True),
            SubmissionObjectResult.review_outcome.is_(None),
        )
        .group_by(Submission.submission_batch_id)
    )
    return {batch_id: count for batch_id, count in db.execute(stmt).all()}


def list_review_required_for_submission(
    db: Session, submission_id: uuid.UUID
) -> list[SubmissionObjectResult]:
    """Every result of one submission flagged ``review_required``, resolved or
    not, with its PageObject, role binding and question config, in ONE query.

    The review page shows what needed a human, including what a human has
    already ruled on: the current ruling is on the row itself
    (``review_outcome`` and friends), so it is not filtered here.
    "Accept all remaining" walks the same set. All three relationships
    are one-to-one, so they are JOINED rather than select-in loaded — one
    statement however many results the submission holds. Unordered: the reading
    order is a rule of the review page, and lives in its service.
    """
    page_object = joinedload(SubmissionObjectResult.page_object)
    stmt = (
        select(SubmissionObjectResult)
        .join(
            SubmissionPage,
            SubmissionPage.id == SubmissionObjectResult.submission_page_id,
        )
        .where(
            SubmissionPage.submission_id == submission_id,
            SubmissionObjectResult.review_required.is_(True),
        )
        .options(
            page_object.joinedload(PageObject.role_binding),
            page_object.selectinload(PageObject.group_role_bindings),
            page_object.joinedload(PageObject.question_config),
        )
    )
    return list(db.scalars(stmt).unique().all())


def list_scorable_for_submission(
    db: Session, submission_id: uuid.UUID
) -> list[SubmissionObjectResult]:
    """Every QUESTION and MATRIX result of one submission, in review or not,
    with its sheet, in ONE query.

    What ``scoring_rules.submission_score`` reads: the sheet gives the physical
    order (the first reading of an object is the one scored), the PageObject
    comes from the variant's own load (``page_object_repo.
    list_scorable_for_variant``), matched by ``page_object_id``. Ordered by
    ``source_page_order, id``, the order of that rule.
    """
    stmt = (
        select(SubmissionObjectResult)
        .join(
            SubmissionPage,
            SubmissionPage.id == SubmissionObjectResult.submission_page_id,
        )
        .where(
            SubmissionPage.submission_id == submission_id,
            SubmissionObjectResult.result_type.in_(
                (SubmissionResultType.QUESTION, SubmissionResultType.MATRIX)
            ),
        )
        .options(joinedload(SubmissionObjectResult.submission_page))
        .order_by(SubmissionPage.source_page_order, SubmissionObjectResult.id)
    )
    return list(db.scalars(stmt).all())


def list_closed_results_for_batch(
    db: Session, batch_id: uuid.UUID
) -> list[SubmissionObjectResult]:
    """Every QUESTION, MATRIX, FREE_TEXT and CODE result of the CLOSED
    submissions of a batch, with its sheet, in ONE query.

    What the Detail export reads: only a closed submission has columns per
    question (an UNRESOLVABLE one leaves them empty). The sheet gives the
    submission (``submission_id``) and the physical order of the first-reading
    rule; the PageObject comes from the variants' own load
    (``page_object_repo.list_exportable_for_variants``). A respondent-ID grid
    (RESPONDENT_IDENTIFIER) is the candidate column, never a column of its own.
    """
    stmt = (
        select(SubmissionObjectResult)
        .join(
            SubmissionPage,
            SubmissionPage.id == SubmissionObjectResult.submission_page_id,
        )
        .join(Submission, Submission.id == SubmissionPage.submission_id)
        .where(
            Submission.submission_batch_id == batch_id,
            Submission.status.in_(CLOSED_SUBMISSION_STATUSES),
            SubmissionObjectResult.result_type.in_(
                (
                    SubmissionResultType.QUESTION,
                    SubmissionResultType.MATRIX,
                    SubmissionResultType.FREE_TEXT,
                    SubmissionResultType.CODE,
                )
            ),
        )
        .options(joinedload(SubmissionObjectResult.submission_page))
        .order_by(
            SubmissionPage.submission_id,
            SubmissionPage.source_page_order,
            SubmissionObjectResult.id,
        )
    )
    return list(db.scalars(stmt).all())


def existing_object_pairs_for_pages(
    db: Session, page_ids: Collection[uuid.UUID]
) -> set[tuple[uuid.UUID, uuid.UUID]]:
    """``{(submission_page_id, page_object_id)}`` already holding a result,
    among *page_ids*, in ONE query.

    The one-submission counterpart of :func:`existing_object_pairs_for_batch`,
    for "Review anyway", which must not read a whole batch's results to act on
    a single submission. Empty input returns an empty set without a query.
    """
    if not page_ids:
        return set()
    stmt = select(
        SubmissionObjectResult.submission_page_id,
        SubmissionObjectResult.page_object_id,
    ).where(SubmissionObjectResult.submission_page_id.in_(page_ids))
    return {(page_id, object_id) for page_id, object_id in db.execute(stmt).all()}


# ---------------------------------------------------------------------------
# Review actions
# ---------------------------------------------------------------------------


def get_for_submission(
    db: Session, submission_id: uuid.UUID, result_id: uuid.UUID
) -> SubmissionObjectResult | None:
    """One result, ONLY if it belongs to *submission_id*, with everything a
    review action needs to judge and validate it, in a constant number of
    statements.

    The PageObject, its role binding and its QUESTION / MATRIX config are
    joined (one-to-one); a question's options come in one select-in. None when
    the result does not exist or belongs to another submission — the caller
    cannot tell the two apart, and should not.
    """
    page_object = joinedload(SubmissionObjectResult.page_object)
    stmt = (
        select(SubmissionObjectResult)
        .join(
            SubmissionPage,
            SubmissionPage.id == SubmissionObjectResult.submission_page_id,
        )
        .where(
            SubmissionObjectResult.id == result_id,
            SubmissionPage.submission_id == submission_id,
        )
        .options(
            page_object.joinedload(PageObject.role_binding),
            page_object.selectinload(PageObject.group_role_bindings),
            page_object.joinedload(PageObject.question_config).selectinload(
                QuestionObjectConfig.options
            ),
            page_object.joinedload(PageObject.matrix_config),
        )
    )
    return db.scalars(stmt).unique().one_or_none()


def add_review(db: Session, review: SubmissionReview) -> SubmissionReview:
    """Stage one log row. Neither flushes nor commits: the action owns the
    transaction, so the log row and the state it records land together."""
    db.add(review)
    return review


def confirm_many(
    db: Session,
    result_ids: Collection[uuid.UUID],
    reviewer_id: uuid.UUID,
    reviewed_at: datetime,
) -> None:
    """CONFIRM every result of *result_ids* and append one log row each, in
    TWO statements however many there are ("Accept all remaining").

    A bulk UPDATE and a bulk INSERT rather than per-row ORM writes, so the
    cost of the action does not grow with the submission. The UPDATE
    synchronises any of those results the session already holds. No note: a
    bulk acceptance has none. Empty input does nothing.
    """
    if not result_ids:
        return
    ids = list(result_ids)
    db.execute(
        update(SubmissionObjectResult)
        .where(SubmissionObjectResult.id.in_(ids))
        .values(
            review_outcome=ReviewOutcome.CONFIRMED,
            reviewed_payload_json=None,
            review_note=None,
            reviewed_by=reviewer_id,
            reviewed_at=reviewed_at,
        )
        .execution_options(synchronize_session="fetch")
    )
    rows: list[dict[str, Any]] = [
        {
            "id": uuid.uuid4(),
            "submission_object_result_id": result_id,
            "reviewer_user_id": reviewer_id,
            "action": ReviewAction.CONFIRMED,
            "review_note": None,
            "reviewed_payload_json": None,
            # The same instant as ``reviewed_at`` (see review_action_service).
            "created_at": reviewed_at,
        }
        for result_id in ids
    ]
    db.execute(insert(SubmissionReview), rows)


# ---------------------------------------------------------------------------
# Candidate from a reading
# ---------------------------------------------------------------------------


def list_respondent_identifier_readings(
    db: Session, submission_ids: Collection[uuid.UUID]
) -> list[SubmissionObjectResult]:
    """Every respondent-ID grid reading (``result_type`` RESPONDENT_IDENTIFIER)
    of the submissions in *submission_ids*, in ONE query.

    With the sheet (for its ``submission_id``) and the PageObject with its role
    binding joined, which is what ``review_rules.candidate_from_reading``
    reads. Ordered by submission and then by physical page order, so "the first
    reading" means the same thing as for a RID at upload. Only what is visible
    to the session: the caller flushes what it staged. Empty input returns an
    empty list without a query.
    """
    if not submission_ids:
        return []
    stmt = (
        select(SubmissionObjectResult)
        .join(
            SubmissionPage,
            SubmissionPage.id == SubmissionObjectResult.submission_page_id,
        )
        .where(
            SubmissionPage.submission_id.in_(list(submission_ids)),
            SubmissionObjectResult.result_type
            == SubmissionResultType.RESPONDENT_IDENTIFIER,
        )
        .options(
            joinedload(SubmissionObjectResult.submission_page),
            joinedload(SubmissionObjectResult.page_object).joinedload(
                PageObject.role_binding
            ),
            joinedload(SubmissionObjectResult.page_object).selectinload(
                PageObject.group_role_bindings
            ),
        )
        .order_by(
            SubmissionPage.submission_id,
            SubmissionPage.source_page_order,
            SubmissionObjectResult.id,
        )
    )
    return list(db.scalars(stmt).unique().all())


def list_identifier_readings_for_submission(
    db: Session, submission_id: uuid.UUID
) -> list[SubmissionObjectResult]:
    """Every result of one submission that MAY read its candidate identifier —
    a respondent-ID grid (``result_type`` RESPONDENT_IDENTIFIER) or a free
    text, in review or not — in ONE query, plus the select-in of the group
    bindings.

    Whether a free text holds RESPONDENT_IDENTIFIER is its PageObject's
    effective role, a rule of the model rather than a column: the caller
    filters with ``review_rules`` (``first_identifier_reading``). With the
    sheet (for its physical order) and the PageObject with its own and its
    group's role bindings loaded, which is what that rule reads. Unordered:
    the order is the rule's.
    """
    page_object = joinedload(SubmissionObjectResult.page_object)
    stmt = (
        select(SubmissionObjectResult)
        .join(
            SubmissionPage,
            SubmissionPage.id == SubmissionObjectResult.submission_page_id,
        )
        .where(
            SubmissionPage.submission_id == submission_id,
            SubmissionObjectResult.result_type.in_(
                (
                    SubmissionResultType.RESPONDENT_IDENTIFIER,
                    SubmissionResultType.FREE_TEXT,
                )
            ),
        )
        .options(
            joinedload(SubmissionObjectResult.submission_page),
            page_object.joinedload(PageObject.role_binding),
            page_object.selectinload(PageObject.group_role_bindings),
        )
    )
    return list(db.scalars(stmt).unique().all())
