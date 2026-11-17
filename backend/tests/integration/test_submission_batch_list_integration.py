# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for the project batch list.

``GET /form-projects/{project_id}/submission-batches`` lists a project's
batches, newest first, each with metrics computed on the read. What this suite
pins down:

  * scoping and authorisation: only this project's batches; a non-member gets
    the same 403 as everywhere else; a CLOSED project still lists;
  * the per-status counts, with REVIEWED and FINALIZED both counted as
    reviewed;
  * ``ready_for_export``: never for an empty batch, never while any submission
    sits in a blocking status, yes once every one is closed;
  * ``pending_review_count``: results flagged and not reviewed, summed per
    batch, minus the results of submissions already closed;
  * the batch fields, effective threshold included, are ``to_batch_read``'s;
  * three queries per request, however many batches.

REVIEWED / FINALIZED / SCANNED are set directly on the rows, as the
extractors' outputs are in the other suites.
Skipped without TEST_DATABASE_URL.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import event

from app.api.v1.endpoints.submission_batches import list_submission_batches
from app.models.enums import (
    FormProjectStatus,
    FormProjectType,
    FormVariantStatus,
    PageObjectType,
    ProjectRole,
    ReviewAction,
    ReviewOutcome,
    SubmissionResultType,
    SubmissionStatus,
    UserRole,
)
from app.models.form_project import FormProject
from app.models.form_project_member import FormProjectMember
from app.models.form_variant import FormVariant
from app.models.form_variant_page import FormVariantPage
from app.models.page_object import PageObject
from app.models.submission import Submission
from app.models.submission_batch import SubmissionBatch
from app.models.submission_object_result import SubmissionObjectResult
from app.models.submission_page import SubmissionPage
from app.models.submission_review import SubmissionReview
from app.models.user import User
from app.schemas.submission import SubmissionBatchListItem, SubmissionBatchRead
from app.services import submission_batch_service


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_user(db) -> User:
    u = User(
        email=f"batch-list-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.USER,
    )
    db.add(u)
    db.flush()
    return u


def _make_project(
    db, owner: User, *, status: FormProjectStatus = FormProjectStatus.ACTIVE
) -> FormProject:
    project = FormProject(
        owner_id=owner.id,
        type=FormProjectType.EXAM,
        name="Batch-list project",
        status=status,
    )
    db.add(project)
    db.flush()
    db.add(
        FormProjectMember(
            project_id=project.id, user_id=owner.id, role=ProjectRole.OWNER
        )
    )
    db.flush()
    return project


@pytest.fixture
def actor(db) -> User:
    return _make_user(db)


@pytest.fixture
def project(db, actor) -> FormProject:
    return _make_project(db, actor)


@pytest.fixture
def template_page(db, project, actor) -> FormVariantPage:
    """The authored page every result's PageObject hangs off."""
    variant = FormVariant(
        form_project_id=project.id,
        root_key="root",
        name="v1",
        status=FormVariantStatus.PUBLISHED,
        version_number=1,
        created_by_user_id=actor.id,
    )
    db.add(variant)
    db.flush()
    page = FormVariantPage(form_variant_id=variant.id, page_index=1)
    db.add(page)
    db.flush()
    return page


def _make_batch(
    db,
    project,
    owner,
    *,
    name: str = "Batch",
    threshold: float | None = None,
    created_at: datetime | None = None,
) -> SubmissionBatch:
    batch = SubmissionBatch(
        form_project_id=project.id,
        name=name,
        created_by_user_id=owner.id,
        confidence_threshold=threshold,
    )
    # Every row of one test shares the transaction's now(); ordering tests set
    # the timestamp explicitly so "newest" means something.
    if created_at is not None:
        batch.created_at = created_at
    db.add(batch)
    db.flush()
    return batch


def _make_submission(
    db,
    batch,
    template_page: FormVariantPage,
    status: SubmissionStatus = SubmissionStatus.MATCHED,
) -> tuple[Submission, SubmissionPage]:
    """A one-sheet submission, matched to the template's variant unless its
    status says matching never resolved one."""
    matched = status not in (
        SubmissionStatus.MATCH_FAILED,
        SubmissionStatus.UNASSIGNED,
        SubmissionStatus.UNRESOLVABLE,
    )
    submission = Submission(
        submission_batch_id=batch.id,
        matched_form_variant_id=template_page.form_variant_id if matched else None,
        status=status,
    )
    db.add(submission)
    db.flush()
    sheet = SubmissionPage(
        submission_id=submission.id,
        source_file_path=f"/tmp/{uuid.uuid4().hex}.png",
        source_filename="page0.png",
        source_page_order=0,
        form_variant_page_id=template_page.id if matched else None,
    )
    db.add(sheet)
    db.flush()
    return submission, sheet


def _make_result(
    db, sheet: SubmissionPage, template_page: FormVariantPage, *, review_required: bool
) -> SubmissionObjectResult:
    """One result on *sheet*, on its OWN authored object — results are unique
    per ``(submission_page_id, page_object_id)``."""
    obj = PageObject(
        form_variant_page_id=template_page.id,
        object_type=PageObjectType.QUESTION,
        x=0,
        y=0,
        width=10,
        height=10,
    )
    db.add(obj)
    db.flush()
    result = SubmissionObjectResult(
        submission_page_id=sheet.id,
        page_object_id=obj.id,
        result_type=SubmissionResultType.QUESTION,
        review_required=review_required,
    )
    db.add(result)
    db.flush()
    return result


def _review(
    db,
    result: SubmissionObjectResult,
    reviewer: User,
    action: ReviewAction = ReviewAction.CONFIRMED,
) -> None:
    """A review action as ``review_action_service`` writes it: the log row and
    the current ruling (cleared by UNDONE)."""
    db.add(
        SubmissionReview(
            submission_object_result_id=result.id,
            reviewer_user_id=reviewer.id,
            action=action,
            review_note="looked at it",
        )
    )
    result.review_outcome = (
        None if action is ReviewAction.UNDONE else ReviewOutcome(action.value)
    )
    db.flush()


def _list(db, project, user) -> list[SubmissionBatchListItem]:
    return list_submission_batches(project_id=project.id, db=db, user=user)


def _by_id(db, project, user) -> dict[uuid.UUID, SubmissionBatchListItem]:
    return {item.id: item for item in _list(db, project, user)}


# ---------------------------------------------------------------------------
# Scoping, authorisation, ordering
# ---------------------------------------------------------------------------
def test_a_project_without_batches_lists_nothing(db, actor, project):
    assert _list(db, project, actor) == []


def test_a_non_member_is_refused_like_everywhere_else(db, actor, project):
    _make_batch(db, project, actor)
    stranger = _make_user(db)

    with pytest.raises(HTTPException) as exc:
        _list(db, project, stranger)

    assert exc.value.status_code == 403


def test_an_unknown_project_is_404(db, actor):
    with pytest.raises(HTTPException) as exc:
        list_submission_batches(project_id=uuid.uuid4(), db=db, user=actor)

    assert exc.value.status_code == 404


def test_batches_of_another_project_are_not_listed(db, actor, project):
    mine = _make_batch(db, project, actor)
    other_project = _make_project(db, actor)
    _make_batch(db, other_project, actor)

    assert [item.id for item in _list(db, project, actor)] == [mine.id]


@pytest.mark.parametrize(
    "project_status", [FormProjectStatus.CLOSED, FormProjectStatus.ARCHIVED]
)
def test_a_concluded_project_still_lists_its_batches(db, actor, project_status):
    """A read: the ACTIVE gate that stops new sheets being collected does not
    apply to looking at what was collected."""
    project = _make_project(db, actor, status=project_status)
    batch = _make_batch(db, project, actor)

    assert [item.id for item in _list(db, project, actor)] == [batch.id]


def test_batches_are_listed_newest_first(db, actor, project):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    oldest = _make_batch(db, project, actor, name="old", created_at=base)
    newest = _make_batch(
        db, project, actor, name="new", created_at=base + timedelta(days=2)
    )
    middle = _make_batch(
        db, project, actor, name="mid", created_at=base + timedelta(days=1)
    )

    assert [item.id for item in _list(db, project, actor)] == [
        newest.id,
        middle.id,
        oldest.id,
    ]


def test_same_instant_batches_fall_back_to_id_descending(db, actor, project):
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    batches = [_make_batch(db, project, actor, created_at=at) for _ in range(3)]

    assert [item.id for item in _list(db, project, actor)] == sorted(
        (b.id for b in batches), reverse=True
    )


# ---------------------------------------------------------------------------
# Per-status counts
# ---------------------------------------------------------------------------
def test_status_counts(db, actor, project, template_page):
    batch = _make_batch(db, project, actor)
    for status in (
        SubmissionStatus.MATCHED,
        SubmissionStatus.MATCH_FAILED,
        SubmissionStatus.MATCH_FAILED,
        SubmissionStatus.UNASSIGNED,
        SubmissionStatus.REVIEWED,
        SubmissionStatus.FINALIZED,
        SubmissionStatus.FINALIZED,
        SubmissionStatus.SCANNED,
        SubmissionStatus.UPLOADED,
    ):
        _make_submission(db, batch, template_page, status)

    item = _by_id(db, project, actor)[batch.id]

    assert item.total_submissions == 9
    assert item.match_failed_count == 2
    assert item.unassigned_count == 1
    assert item.reviewed_count == 3  # REVIEWED + FINALIZED


def test_counts_are_per_batch(db, actor, project, template_page):
    first = _make_batch(db, project, actor)
    second = _make_batch(db, project, actor)
    _make_submission(db, first, template_page, SubmissionStatus.MATCH_FAILED)
    for _ in range(3):
        _make_submission(db, second, template_page, SubmissionStatus.UNASSIGNED)

    items = _by_id(db, project, actor)

    assert (items[first.id].total_submissions, items[first.id].match_failed_count) == (1, 1)
    assert items[first.id].unassigned_count == 0
    assert (items[second.id].total_submissions, items[second.id].unassigned_count) == (3, 3)
    assert items[second.id].match_failed_count == 0


def test_an_empty_batch_reports_zeros(db, actor, project):
    batch = _make_batch(db, project, actor)

    item = _by_id(db, project, actor)[batch.id]

    assert item.total_submissions == 0
    assert item.match_failed_count == 0
    assert item.unassigned_count == 0
    assert item.unresolvable_count == 0
    assert item.reviewed_count == 0
    assert item.pending_review_count == 0


# ---------------------------------------------------------------------------
# ready_for_export
# ---------------------------------------------------------------------------
def test_an_empty_batch_is_not_ready_for_export(db, actor, project):
    batch = _make_batch(db, project, actor)

    assert _by_id(db, project, actor)[batch.id].ready_for_export is False


@pytest.mark.parametrize(
    "blocking",
    [
        SubmissionStatus.UPLOADED,
        SubmissionStatus.MATCHED,
        SubmissionStatus.MATCH_FAILED,
        SubmissionStatus.UNASSIGNED,
        SubmissionStatus.SCANNED,
    ],
)
def test_one_blocking_submission_holds_the_batch_back(
    db, actor, project, template_page, blocking
):
    batch = _make_batch(db, project, actor)
    _make_submission(db, batch, template_page, SubmissionStatus.REVIEWED)
    _make_submission(db, batch, template_page, SubmissionStatus.FINALIZED)
    _make_submission(db, batch, template_page, blocking)

    assert _by_id(db, project, actor)[batch.id].ready_for_export is False


def test_a_batch_whose_submissions_are_all_closed_is_ready(
    db, actor, project, template_page
):
    batch = _make_batch(db, project, actor)
    _make_submission(db, batch, template_page, SubmissionStatus.REVIEWED)
    _make_submission(db, batch, template_page, SubmissionStatus.FINALIZED)

    assert _by_id(db, project, actor)[batch.id].ready_for_export is True


def test_an_unresolvable_submission_does_not_hold_the_batch_back(
    db, actor, project, template_page
):
    """An UNRESOLVABLE submission is closed and does not block the export. It
    is counted on its own, not as reviewed."""
    batch = _make_batch(db, project, actor)
    _make_submission(db, batch, template_page, SubmissionStatus.REVIEWED)
    _make_submission(db, batch, template_page, SubmissionStatus.UNRESOLVABLE)

    item = _by_id(db, project, actor)[batch.id]

    assert item.ready_for_export is True
    assert item.unresolvable_count == 1
    assert item.reviewed_count == 1
    assert item.match_failed_count == 0


def test_a_batch_of_unresolvable_submissions_only_is_not_ready(
    db, actor, project, template_page
):
    """Nothing to export, like an empty batch."""
    batch = _make_batch(db, project, actor)
    _make_submission(db, batch, template_page, SubmissionStatus.UNRESOLVABLE)
    _make_submission(db, batch, template_page, SubmissionStatus.UNRESOLVABLE)

    assert _by_id(db, project, actor)[batch.id].ready_for_export is False


def test_is_ready_for_export_rule():
    """The rule on its own, as the export calls it."""
    ready = submission_batch_service._is_ready_for_export
    assert ready({}) is False
    assert ready({SubmissionStatus.REVIEWED: 0}) is False
    assert ready({SubmissionStatus.REVIEWED: 2, SubmissionStatus.FINALIZED: 1}) is True
    assert ready({SubmissionStatus.REVIEWED: 2, SubmissionStatus.MATCHED: 1}) is False
    # A zero count for a blocking status is not a blocking submission.
    assert ready({SubmissionStatus.FINALIZED: 1, SubmissionStatus.MATCHED: 0}) is True
    # UNRESOLVABLE never blocks, but alone it is nothing to export.
    assert ready({SubmissionStatus.REVIEWED: 1, SubmissionStatus.UNRESOLVABLE: 3}) is True
    assert ready({SubmissionStatus.UNRESOLVABLE: 3}) is False
    assert (
        ready({SubmissionStatus.UNRESOLVABLE: 1, SubmissionStatus.MATCH_FAILED: 1})
        is False
    )


# ---------------------------------------------------------------------------
# pending_review_count
# ---------------------------------------------------------------------------
def test_pending_counts_flagged_unreviewed_results_across_submissions(
    db, actor, project, template_page
):
    batch = _make_batch(db, project, actor)
    _, first = _make_submission(db, batch, template_page)
    _, second = _make_submission(db, batch, template_page, SubmissionStatus.SCANNED)
    _make_result(db, first, template_page, review_required=True)
    _make_result(db, first, template_page, review_required=False)
    _make_result(db, second, template_page, review_required=True)
    _make_result(db, second, template_page, review_required=True)

    assert _by_id(db, project, actor)[batch.id].pending_review_count == 3


def test_an_undone_result_is_pending_again(db, actor, project, template_page):
    """Pending follows ``review_outcome``, not the review log."""
    batch = _make_batch(db, project, actor)
    _, sheet = _make_submission(db, batch, template_page)
    undone = _make_result(db, sheet, template_page, review_required=True)
    flagged = _make_result(db, sheet, template_page, review_required=True)
    _review(db, undone, actor)
    _review(db, undone, actor, ReviewAction.UNDONE)
    _review(db, flagged, actor, ReviewAction.FLAGGED)

    assert _by_id(db, project, actor)[batch.id].pending_review_count == 1


def test_a_reviewed_result_is_not_pending(db, actor, project, template_page):
    batch = _make_batch(db, project, actor)
    _, sheet = _make_submission(db, batch, template_page)
    _make_result(db, sheet, template_page, review_required=True)
    reviewed = _make_result(db, sheet, template_page, review_required=True)
    _review(db, reviewed, actor)

    assert _by_id(db, project, actor)[batch.id].pending_review_count == 1


@pytest.mark.parametrize(
    "closed", [SubmissionStatus.REVIEWED, SubmissionStatus.FINALIZED]
)
def test_results_of_closed_submissions_are_not_pending(
    db, actor, project, template_page, closed
):
    """Still flagged and never reviewed, but the submission is closed: no work
    left there, so it does not count."""
    batch = _make_batch(db, project, actor)
    _, open_sheet = _make_submission(db, batch, template_page)
    _, closed_sheet = _make_submission(db, batch, template_page, closed)
    _make_result(db, open_sheet, template_page, review_required=True)
    _make_result(db, closed_sheet, template_page, review_required=True)
    _make_result(db, closed_sheet, template_page, review_required=True)

    assert _by_id(db, project, actor)[batch.id].pending_review_count == 1


def test_a_batch_without_results_has_nothing_pending(
    db, actor, project, template_page
):
    batch = _make_batch(db, project, actor)
    _make_submission(db, batch, template_page)
    _make_submission(db, batch, template_page, SubmissionStatus.MATCH_FAILED)

    assert _by_id(db, project, actor)[batch.id].pending_review_count == 0


def test_pending_counts_are_per_batch(db, actor, project, template_page):
    busy = _make_batch(db, project, actor)
    quiet = _make_batch(db, project, actor)
    _, busy_sheet = _make_submission(db, busy, template_page)
    _, quiet_sheet = _make_submission(db, quiet, template_page)
    for _ in range(2):
        _make_result(db, busy_sheet, template_page, review_required=True)
    _make_result(db, quiet_sheet, template_page, review_required=False)

    items = _by_id(db, project, actor)

    assert items[busy.id].pending_review_count == 2
    assert items[quiet.id].pending_review_count == 0


# ---------------------------------------------------------------------------
# Batch fields and threshold
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("threshold", [None, 0.42])
def test_batch_fields_match_to_batch_read(db, actor, project, threshold):
    batch = _make_batch(db, project, actor, threshold=threshold)

    item = _by_id(db, project, actor)[batch.id]
    expected = submission_batch_service.to_batch_read(batch)

    assert item.confidence_threshold == threshold
    assert item.effective_threshold == expected.effective_threshold
    # Every inherited batch field, not just the threshold pair.
    assert (
        item.model_dump(include=set(SubmissionBatchRead.model_fields))
        == expected.model_dump()
    )


# ---------------------------------------------------------------------------
# Query budget
# ---------------------------------------------------------------------------
def test_three_queries_however_many_batches(db, actor, project, template_page):
    """Authorisation aside, one query for the batches and one per metric —
    never one per batch."""
    for _ in range(4):
        batch = _make_batch(db, project, actor)
        _, sheet = _make_submission(db, batch, template_page)
        _make_result(db, sheet, template_page, review_required=True)

    statements: list[str] = []

    def _record(conn, cursor, statement, *args):
        statements.append(statement)

    engine = db.get_bind().engine
    event.listen(engine, "before_cursor_execute", _record)
    try:
        _list(db, project, actor)
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    data_queries = [
        s
        for s in statements
        if "submission_batches" in s or "submission_object_results" in s
    ]
    assert len(data_queries) == 3
