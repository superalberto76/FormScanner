# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for the per-submission pending-review count.

``GET /submission-batches/{batch_id}`` reports, on every ``SubmissionSummary``,
how many of that submission's results still await a human. What this suite pins
down:

  * "pending" is ``review_required`` AND ``review_outcome IS NULL`` — a result
    a human already ruled on no longer counts, whatever its flag says; an
    UNDONE one counts again;
  * a submission with only clean results, or with no results at all
    (UNASSIGNED, MATCH_FAILED), reports 0 — the latter with no explicit
    exclusion anywhere, simply because nothing joins;
  * the count is populated for every row of the list, and for the filtered
    subset when ``?status=`` is used;
  * **the count is live**: moving the batch threshold is reflected by the very
    next detail read, with no extra step — nothing is cached or stored;
  * the pre-existing summary fields are unchanged.

Results are built directly via the ORM, in the states the extractors leave them
in, as in the confidence-threshold suite. Skipped without TEST_DATABASE_URL.
"""
from __future__ import annotations

import uuid

import pytest

from app.api.v1.endpoints.submission_batches import (
    get_submission_batch,
    update_batch_confidence_threshold,
)
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
from app.repositories import submission_batch_repo, submission_object_result_repo
from app.schemas.submission import ConfidenceThresholdUpdate, SubmissionSummary


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


@pytest.fixture
def actor(db) -> User:
    u = User(
        email=f"pending-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.USER,
    )
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def project(db, actor) -> FormProject:
    project = FormProject(
        owner_id=actor.id,
        type=FormProjectType.EXAM,
        name="Pending-review project",
        status=FormProjectStatus.ACTIVE,
    )
    db.add(project)
    db.flush()
    db.add(
        FormProjectMember(
            project_id=project.id, user_id=actor.id, role=ProjectRole.OWNER
        )
    )
    db.flush()
    return project


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


def _make_batch(db, project, owner, *, threshold: float | None = None) -> SubmissionBatch:
    batch = SubmissionBatch(
        form_project_id=project.id,
        name="Batch 1",
        created_by_user_id=owner.id,
        confidence_threshold=threshold,
    )
    db.add(batch)
    db.flush()
    return batch


def _make_submission(
    db,
    batch,
    template_page: FormVariantPage,
    *,
    status: SubmissionStatus = SubmissionStatus.MATCHED,
    pages: int = 1,
) -> tuple[Submission, list[SubmissionPage]]:
    """A submission of *pages* sheets. Matched to the template's variant unless
    its status says matching never resolved one."""
    matched = status not in (SubmissionStatus.MATCH_FAILED, SubmissionStatus.UNASSIGNED)
    submission = Submission(
        submission_batch_id=batch.id,
        matched_form_variant_id=template_page.form_variant_id if matched else None,
        status=status,
    )
    db.add(submission)
    db.flush()
    sheets = []
    for order in range(pages):
        sheet = SubmissionPage(
            submission_id=submission.id,
            source_file_path=f"/tmp/{uuid.uuid4().hex}.png",
            source_filename=f"page{order}.png",
            source_page_order=order,
            form_variant_page_id=template_page.id if matched else None,
        )
        db.add(sheet)
        sheets.append(sheet)
    db.flush()
    return submission, sheets


def _make_result(
    db,
    sheet: SubmissionPage,
    template_page: FormVariantPage,
    *,
    review_required: bool,
    confidence_score: float | None = None,
) -> SubmissionObjectResult:
    """One result on *sheet*, pointing at its OWN authored object — results are
    unique per ``(submission_page_id, page_object_id)``."""
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
        confidence_score=confidence_score,
        review_required=review_required,
    )
    db.add(result)
    db.flush()
    return result


def _review(db, result: SubmissionObjectResult, reviewer: User) -> SubmissionReview:
    """A CONFIRM as ``review_action_service`` writes it: log row + ruling."""
    review = SubmissionReview(
        submission_object_result_id=result.id,
        reviewer_user_id=reviewer.id,
        action=ReviewAction.CONFIRMED,
        review_note="looked at it",
    )
    db.add(review)
    result.review_outcome = ReviewOutcome.CONFIRMED
    result.reviewed_by = reviewer.id
    db.flush()
    return review


def _undo(db, result: SubmissionObjectResult, reviewer: User) -> None:
    db.add(
        SubmissionReview(
            submission_object_result_id=result.id,
            reviewer_user_id=reviewer.id,
            action=ReviewAction.UNDONE,
        )
    )
    result.review_outcome = None
    result.reviewed_by = None
    db.flush()


def _summaries(db, batch, actor, status=None) -> dict[uuid.UUID, SubmissionSummary]:
    detail = get_submission_batch(batch_id=batch.id, status=status, db=db, user=actor)
    return {s.id: s for s in detail.submissions}


# ---------------------------------------------------------------------------
# What counts as pending
# ---------------------------------------------------------------------------
def test_a_reviewed_result_is_not_pending_even_if_flagged(db, actor, project, template_page):
    """Two flagged and unreviewed, one flagged but already reviewed: 2, not 3.
    The current ruling is what takes a result out of the queue, not the flag."""
    batch = _make_batch(db, project, actor)
    submission, (sheet,) = _make_submission(db, batch, template_page)
    _make_result(db, sheet, template_page, review_required=True)
    _make_result(db, sheet, template_page, review_required=True)
    reviewed = _make_result(db, sheet, template_page, review_required=True)
    _review(db, reviewed, actor)

    assert submission_object_result_repo.pending_review_counts_for_batch(
        db, batch.id
    ) == {submission.id: 2}
    assert _summaries(db, batch, actor)[submission.id].pending_review_count == 2


def test_results_are_counted_across_every_sheet_of_the_submission(
    db, actor, project, template_page
):
    """The count is per SUBMISSION, not per sheet: the GROUP BY is on the
    submission the sheets belong to."""
    batch = _make_batch(db, project, actor)
    submission, (first, second) = _make_submission(db, batch, template_page, pages=2)
    _make_result(db, first, template_page, review_required=True)
    _make_result(db, second, template_page, review_required=True)
    _make_result(db, second, template_page, review_required=True)

    assert _summaries(db, batch, actor)[submission.id].pending_review_count == 3


def test_a_submission_with_only_clean_results_reports_zero(db, actor, project, template_page):
    batch = _make_batch(db, project, actor)
    submission, (sheet,) = _make_submission(db, batch, template_page)
    _make_result(db, sheet, template_page, review_required=False)
    _make_result(db, sheet, template_page, review_required=False)

    assert submission_object_result_repo.pending_review_counts_for_batch(
        db, batch.id
    ) == {}
    assert _summaries(db, batch, actor)[submission.id].pending_review_count == 0


@pytest.mark.parametrize(
    "status", [SubmissionStatus.UNASSIGNED, SubmissionStatus.MATCH_FAILED]
)
def test_a_submission_without_results_reports_zero(
    db, actor, project, template_page, status
):
    """Never extracted (extraction needs a matched variant), so nothing joins —
    0 falls out of the query naturally, with no status exclusion in the code."""
    batch = _make_batch(db, project, actor)
    submission, _ = _make_submission(db, batch, template_page, status=status)

    summaries = _summaries(db, batch, actor)

    assert summaries[submission.id].pending_review_count == 0


def test_results_of_another_batch_are_not_counted(db, actor, project, template_page):
    batch = _make_batch(db, project, actor)
    other = _make_batch(db, project, actor)
    mine, (my_sheet,) = _make_submission(db, batch, template_page)
    theirs, (their_sheet,) = _make_submission(db, other, template_page)
    _make_result(db, my_sheet, template_page, review_required=True)
    _make_result(db, their_sheet, template_page, review_required=True)
    _make_result(db, their_sheet, template_page, review_required=True)

    assert submission_object_result_repo.pending_review_counts_for_batch(
        db, batch.id
    ) == {mine.id: 1}


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------
def test_the_detail_populates_the_count_on_every_row(db, actor, project, template_page):
    """Every submission of the list, in list_submissions order, each with its own
    count — including the ones with nothing to review."""
    batch = _make_batch(db, project, actor)
    busy, (busy_sheet,) = _make_submission(db, batch, template_page)
    clean, (clean_sheet,) = _make_submission(db, batch, template_page)
    failed, _ = _make_submission(
        db, batch, template_page, status=SubmissionStatus.MATCH_FAILED
    )
    for _ in range(3):
        _make_result(db, busy_sheet, template_page, review_required=True)
    _make_result(db, clean_sheet, template_page, review_required=False)

    detail = get_submission_batch(batch_id=batch.id, db=db, user=actor)

    assert [s.id for s in detail.submissions] == [
        s.id for s in submission_batch_repo.list_submissions(db, batch.id)
    ]
    assert {s.id: s.pending_review_count for s in detail.submissions} == {
        busy.id: 3,
        clean.id: 0,
        failed.id: 0,
    }


def test_the_status_filter_still_populates_the_count(db, actor, project, template_page):
    """``?status=`` narrows the rows; the count on the rows that
    remain is still each one's own."""
    batch = _make_batch(db, project, actor)
    first, (first_sheet,) = _make_submission(db, batch, template_page)
    second, (second_sheet,) = _make_submission(db, batch, template_page)
    failed, _ = _make_submission(
        db, batch, template_page, status=SubmissionStatus.MATCH_FAILED
    )
    _make_result(db, first_sheet, template_page, review_required=True)
    for _ in range(2):
        _make_result(db, second_sheet, template_page, review_required=True)

    matched = _summaries(db, batch, actor, status=SubmissionStatus.MATCHED)
    assert {sid: s.pending_review_count for sid, s in matched.items()} == {
        first.id: 1,
        second.id: 2,
    }

    only_failed = _summaries(db, batch, actor, status=SubmissionStatus.MATCH_FAILED)
    assert {sid: s.pending_review_count for sid, s in only_failed.items()} == {
        failed.id: 0
    }


def test_the_count_follows_a_threshold_change_immediately(
    db, actor, project, template_page
):
    """THE test of this suite. Move the threshold through the threshold
    endpoint and read the detail right after: the count has already moved,
    both ways, with no extra step — review_required is re-derived there, and
    the count is read live here, never stored. A reviewed result stays out
    throughout."""
    batch = _make_batch(db, project, actor, threshold=0.5)
    submission, (sheet,) = _make_submission(db, batch, template_page)
    # All clear the initial 0.5, so nothing is pending yet.
    _make_result(db, sheet, template_page, confidence_score=0.6, review_required=False)
    _make_result(db, sheet, template_page, confidence_score=0.8, review_required=False)
    _make_result(db, sheet, template_page, confidence_score=0.95, review_required=False)
    shielded = _make_result(
        db, sheet, template_page, confidence_score=0.6, review_required=False
    )
    _review(db, shielded, actor)

    assert _summaries(db, batch, actor)[submission.id].pending_review_count == 0

    def move_to(threshold):
        update_batch_confidence_threshold(
            batch_id=batch.id,
            data=ConfidenceThresholdUpdate(confidence_threshold=threshold),
            db=db,
            user=actor,
        )

    # 0.6 and 0.8 now fall below the bar; the reviewed 0.6 is frozen.
    move_to(0.9)
    assert _summaries(db, batch, actor)[submission.id].pending_review_count == 2

    # Back down: only 0.6 stays below 0.7.
    move_to(0.7)
    assert _summaries(db, batch, actor)[submission.id].pending_review_count == 1

    move_to(0.1)
    assert _summaries(db, batch, actor)[submission.id].pending_review_count == 0


# ---------------------------------------------------------------------------
# No regression on the existing fields
# ---------------------------------------------------------------------------
def test_the_existing_summary_fields_are_unchanged(db, actor, project, template_page):
    batch = _make_batch(db, project, actor)
    matched, (sheet, _) = _make_submission(db, batch, template_page, pages=2)
    failed, _ = _make_submission(
        db, batch, template_page, status=SubmissionStatus.MATCH_FAILED, pages=3
    )
    _make_result(db, sheet, template_page, review_required=True)

    summaries = _summaries(db, batch, actor)

    m = summaries[matched.id]
    assert m.status == SubmissionStatus.MATCHED
    assert m.matched_form_variant_id == template_page.form_variant_id
    assert m.page_count == 2
    assert m.created_at == matched.created_at
    assert m.pending_review_count == 1

    f = summaries[failed.id]
    assert f.status == SubmissionStatus.MATCH_FAILED
    assert f.matched_form_variant_id is None
    assert f.page_count == 3
    assert f.created_at == failed.created_at
    assert f.pending_review_count == 0


def test_an_undone_result_is_pending_again(db, actor, project, template_page):
    """What resolves a result is the current ruling, not the log. An UNDONE
    result has two log rows and no ruling — pending again."""
    batch = _make_batch(db, project, actor)
    submission, (sheet,) = _make_submission(db, batch, template_page)
    undone = _make_result(db, sheet, template_page, review_required=True)
    confirmed = _make_result(db, sheet, template_page, review_required=True)
    _review(db, undone, actor)
    _undo(db, undone, actor)
    _review(db, confirmed, actor)

    assert submission_object_result_repo.pending_review_counts_for_batch(
        db, batch.id
    ) == {submission.id: 1}
    assert _summaries(db, batch, actor)[submission.id].pending_review_count == 1
