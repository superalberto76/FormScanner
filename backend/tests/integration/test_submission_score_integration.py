# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for the submission score.

The review read (``GET /submissions/{id}/review``) carries ``score``, the
triple of ``scoring_rules.submission_score``: points, the variant's maximum
and the percentage. The rules themselves are pinned down by the unit tests of
``test_scoring_rules.py``; what this suite pins down is what the queries hand
them and how the read exposes the result:

  * **the triple** — QUESTIONs and an evaluative MATRIX read from the DB,
    whatever their ``review_required``;
  * **live** — every review action (CONFIRM, SUBSTITUTE, FLAG, UNDO, "Accept
    all remaining") returns the submission with the new score;
  * **which reading** — duplicate sheets and linked copies score the first
    reading in physical page order;
  * **edge cases** — a missing page is worth 0 with the maximum unchanged; a
    maximum of 0, no percentage; no score outside SCANNED / REVIEWED /
    FINALIZED, even with a variant, nor without a variant;
  * **robustness** — a broken reading gives no score, never a failed read;
  * **total_score** — the close writes the raw score the page was showing,
    pending readings at 0, a negative score as it is, 0 with a maximum of 0;
    NULL, with the close still done, when the score cannot be computed; a
    reopen clears it and the next close writes the new value;
  * **cost** — the same statements for 1 and for many valid readings, the
    scoring included, for the read and for the close that writes
    ``total_score``.

Skipped without TEST_DATABASE_URL, like the rest of tests/integration.
"""
from __future__ import annotations

import logging
import uuid

import pytest
from sqlalchemy import event

from app.api.v1.endpoints.submissions import (
    close_submission,
    get_submission_review,
    reopen_submission,
)
from app.models.enums import (
    FormProjectStatus,
    FormProjectType,
    FormVariantStatus,
    MatrixSelectionRule,
    PageObjectType,
    PageOrderSource,
    ProjectRole,
    QuestionType,
    SubmissionResultType,
    SubmissionStatus,
    UserRole,
)
from app.models.form_project import FormProject
from app.models.form_project_member import FormProjectMember
from app.models.form_variant import FormVariant
from app.models.form_variant_page import FormVariantPage
from app.models.matrix_cell_def import MatrixCellDef
from app.models.matrix_object_config import MatrixObjectConfig
from app.models.page_object import PageObject
from app.models.question_object_config import QuestionObjectConfig
from app.models.question_option import QuestionOption
from app.models.submission import Submission
from app.models.submission_batch import SubmissionBatch
from app.models.submission_object_result import SubmissionObjectResult
from app.models.submission_page import SubmissionPage
from app.models.user import User
from app.schemas.submission import (
    ReviewActionType,
    SubmissionCloseRequest,
    SubmissionReopenRequest,
    SubmissionReviewActionRequest,
    SubmissionReviewDetail,
    SubmissionScoreRead,
)
from app.services import review_action_service

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


@pytest.fixture
def actor(db) -> User:
    user = User(
        email=f"score-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.USER,
    )
    db.add(user)
    db.flush()
    return user


@pytest.fixture
def project(db, actor) -> FormProject:
    project = FormProject(
        owner_id=actor.id,
        type=FormProjectType.EXAM,
        name="Score project",
        status=FormProjectStatus.ACTIVE,
    )
    db.add(project)
    db.flush()
    db.add(FormProjectMember(project_id=project.id, user_id=actor.id, role=ProjectRole.OWNER))
    db.flush()
    return project


@pytest.fixture
def batch(db, project, actor) -> SubmissionBatch:
    batch = SubmissionBatch(
        form_project_id=project.id,
        name="Score batch",
        created_by_user_id=actor.id,
        confidence_threshold=0.8,
    )
    db.add(batch)
    db.flush()
    return batch


@pytest.fixture
def pages(db, project, actor) -> list[FormVariantPage]:
    """A PUBLISHED variant of two template pages, indexed 1 and 2."""
    variant = FormVariant(
        form_project_id=project.id,
        root_key=f"root-{uuid.uuid4().hex[:8]}",
        name="A",
        status=FormVariantStatus.PUBLISHED,
        version_number=1,
        created_by_user_id=actor.id,
    )
    db.add(variant)
    db.flush()
    template_pages = [
        FormVariantPage(form_variant_id=variant.id, page_index=index, width_px=794, height_px=1123)
        for index in (1, 2)
    ]
    db.add_all(template_pages)
    db.flush()
    return template_pages


def _object(
    db,
    page: FormVariantPage,
    object_type: PageObjectType,
    *,
    group: uuid.UUID | None = None,
    linked: bool = False,
) -> PageObject:
    obj = PageObject(
        form_variant_page_id=page.id,
        object_type=object_type,
        x=10.0,
        y=10.0,
        width=100.0,
        height=40.0,
        shared_group_id=group,
        shared_across_pages=linked,
    )
    db.add(obj)
    db.flush()
    return obj


def _question(
    db,
    page: FormVariantPage,
    *scores: float | None,
    question_type: QuestionType = QuestionType.SINGLE_CHOICE,
    group: uuid.UUID | None = None,
    linked: bool = False,
) -> tuple[PageObject, list[uuid.UUID]]:
    """A QUESTION and its option ids, in order."""
    obj = _object(db, page, PageObjectType.QUESTION, group=group, linked=linked)
    config = QuestionObjectConfig(
        page_object_id=obj.id, question_type=question_type, question_text="Q"
    )
    db.add(config)
    db.flush()
    options = [
        QuestionOption(
            question_object_config_id=config.id,
            order_index=index,
            option_text=f"option {index}",
            is_correct=score is not None and score > 0,
            score=score,
        )
        for index, score in enumerate(scores)
    ]
    db.add_all(options)
    db.flush()
    return obj, [option.id for option in options]


def _matrix(
    db, page: FormVariantPage, cells: dict[tuple[int, int], float]
) -> PageObject:
    """An evaluative 2x3 MATRIX, one selection per row."""
    obj = _object(db, page, PageObjectType.MATRIX)
    config = MatrixObjectConfig(
        page_object_id=obj.id,
        rows_count=2,
        cols_count=3,
        evaluative=True,
        selection_rule=MatrixSelectionRule.ONE_PER_ROW,
    )
    db.add(config)
    db.flush()
    db.add_all(
        MatrixCellDef(
            matrix_object_config_id=config.id,
            row_index=row,
            col_index=col,
            is_correct=score > 0,
            score=score,
        )
        for (row, col), score in cells.items()
    )
    db.flush()
    return obj


def _submission(
    db,
    batch,
    bound_to: list[FormVariantPage],
    *,
    variant_id: uuid.UUID | None = None,
    status: SubmissionStatus = SubmissionStatus.SCANNED,
) -> tuple[Submission, list[SubmissionPage]]:
    """A submission with one sheet per template page of *bound_to*, in
    order."""
    if variant_id is None and bound_to:
        variant_id = bound_to[0].form_variant_id
    submission = Submission(
        submission_batch_id=batch.id, matched_form_variant_id=variant_id, status=status
    )
    db.add(submission)
    db.flush()
    sheets = [
        SubmissionPage(
            submission_id=submission.id,
            source_file_path=f"/tmp/{uuid.uuid4().hex}.png",
            source_filename=f"page{order}.png",
            source_page_order=order,
            page_order_source=PageOrderSource.CODE,
            form_variant_page_id=template_page.id,
            match_confidence=1.0,
        )
        for order, template_page in enumerate(bound_to)
    ]
    db.add_all(sheets)
    db.flush()
    return submission, sheets


def _answer(
    db,
    sheet: SubmissionPage,
    obj: PageObject,
    *option_ids: uuid.UUID,
    review_required: bool = False,
) -> SubmissionObjectResult:
    """A QUESTION reading of *obj* on *sheet* with *option_ids* marked."""
    result = SubmissionObjectResult(
        submission_page_id=sheet.id,
        page_object_id=obj.id,
        result_type=SubmissionResultType.QUESTION,
        detected_payload_json={
            "selected_options": [
                {"option_id": str(option_id), "confidence": 0.95} for option_id in option_ids
            ],
            "validity": "VALID" if option_ids else "NO_ANSWER",
        },
        confidence_score=0.5 if review_required else 0.95,
        validity_forces_review=False,
        review_required=review_required,
    )
    db.add(result)
    db.flush()
    return result


def _score(db, submission, user) -> SubmissionScoreRead | None:
    db.expire_all()
    return get_submission_review(submission_id=submission.id, db=db, user=user).score


def _act(
    db, submission, result, user, action: ReviewActionType, value: dict | None = None
) -> SubmissionReviewDetail:
    db.refresh(submission)
    return review_action_service.apply_action(
        db,
        submission.id,
        result.id,
        user,
        SubmissionReviewActionRequest(
            action=action, review_version=submission.review_version, value=value
        ),
    )


def _close(db, submission, user) -> SubmissionReviewDetail:
    db.refresh(submission)
    return close_submission(
        submission_id=submission.id,
        data=SubmissionCloseRequest(review_version=submission.review_version),
        db=db,
        user=user,
    )


def _reopen(db, submission, user) -> SubmissionReviewDetail:
    db.refresh(submission)
    return reopen_submission(
        submission_id=submission.id,
        data=SubmissionReopenRequest(review_version=submission.review_version),
        db=db,
        user=user,
    )


def _total_score(db, submission) -> float | None:
    db.expire_all()
    return db.get(Submission, submission.id).total_score


# ---------------------------------------------------------------------------
# The triple
# ---------------------------------------------------------------------------


def test_the_read_carries_the_score_triple(db, actor, batch, pages):
    single, single_ids = _question(db, pages[0], 2.0, 0.0)
    multi, multi_ids = _question(
        db, pages[1], 1.0, 1.0, 1.0, question_type=QuestionType.MULTIPLE_CHOICE
    )
    matrix = _matrix(db, pages[1], {(0, 0): 1.0, (1, 1): 1.0})
    submission, sheets = _submission(db, batch, pages)
    _answer(db, sheets[0], single, single_ids[0])
    # Pending: worth 0 until someone rules on it.
    _answer(db, sheets[1], multi, multi_ids[0], review_required=True)
    db.add(
        SubmissionObjectResult(
            submission_page_id=sheets[1].id,
            page_object_id=matrix.id,
            result_type=SubmissionResultType.MATRIX,
            detected_payload_json={
                "rows": [
                    {"row_index": 0, "selected_cells": [{"col_index": 0, "confidence": 0.9}], "validity": "VALID"},
                    {"row_index": 1, "selected_cells": [], "validity": "NO_ANSWER"},
                ]
            },
            confidence_score=0.95,
            validity_forces_review=False,
            review_required=False,
        )
    )
    db.flush()

    # Max: 2 + (1 + 1 + 1 - 1, FULL_ANSWER) + (1 + 1) = 6; points: 2 + 0 + 1.
    assert _score(db, submission, actor) == SubmissionScoreRead(
        raw_score=3.0, max_possible_score=6.0, percentage=50.0
    )


def test_a_review_action_returns_the_new_score(db, actor, batch, pages):
    question, ids = _question(db, pages[0], 2.0, 0.0)
    submission, sheets = _submission(db, batch, pages[:1])
    pending = _answer(db, sheets[0], question, ids[0], review_required=True)
    assert _score(db, submission, actor).raw_score == 0.0

    detail = review_action_service.apply_action(
        db,
        submission.id,
        pending.id,
        actor,
        SubmissionReviewActionRequest(
            action=ReviewActionType.CONFIRM, review_version=submission.review_version
        ),
    )
    assert detail.score == SubmissionScoreRead(
        raw_score=2.0, max_possible_score=2.0, percentage=100.0
    )


def test_a_substitution_returns_the_new_score(db, actor, batch, pages):
    question, ids = _question(db, pages[0], 2.0, 0.0)
    submission, sheets = _submission(db, batch, pages[:1])
    wrong = _answer(db, sheets[0], question, ids[1], review_required=True)

    detail = _act(
        db, submission, wrong, actor, ReviewActionType.SUBSTITUTE,
        {"selected_option_ids": [str(ids[0])]},
    )
    assert detail.score.raw_score == 2.0


def test_a_flag_returns_the_new_score(db, actor, batch, pages):
    question, ids = _question(db, pages[0], 2.0, 0.0)
    submission, sheets = _submission(db, batch, pages[:1])
    read = _answer(db, sheets[0], question, ids[0], review_required=True)
    # Confirmed first, so the FLAG is what takes the points away.
    assert _act(db, submission, read, actor, ReviewActionType.CONFIRM).score.raw_score == 2.0

    detail = _act(db, submission, read, actor, ReviewActionType.FLAG)
    assert detail.score.raw_score == 0.0


def test_an_undo_returns_the_new_score(db, actor, batch, pages):
    question, ids = _question(db, pages[0], 2.0, 0.0)
    submission, sheets = _submission(db, batch, pages[:1])
    read = _answer(db, sheets[0], question, ids[0], review_required=True)
    assert _act(db, submission, read, actor, ReviewActionType.CONFIRM).score.raw_score == 2.0

    # Back to pending: worth 0 again.
    detail = _act(db, submission, read, actor, ReviewActionType.UNDO)
    assert detail.score.raw_score == 0.0


def test_accept_all_remaining_returns_the_new_score(db, actor, batch, pages):
    first, first_ids = _question(db, pages[0], 2.0, 0.0)
    second, second_ids = _question(db, pages[0], 3.0, 0.0)
    submission, sheets = _submission(db, batch, pages[:1])
    _answer(db, sheets[0], first, first_ids[0], review_required=True)
    _answer(db, sheets[0], second, second_ids[0], review_required=True)
    assert _score(db, submission, actor).raw_score == 0.0

    db.refresh(submission)
    detail = review_action_service.accept_remaining(
        db, submission.id, actor, submission.review_version
    )
    assert detail.accepted_count == 2
    assert detail.score == SubmissionScoreRead(
        raw_score=5.0, max_possible_score=5.0, percentage=100.0
    )


# ---------------------------------------------------------------------------
# Which reading
# ---------------------------------------------------------------------------


def test_a_duplicate_sheet_scores_its_first_reading(db, actor, batch, pages):
    question, ids = _question(db, pages[0], 2.0, 0.0)
    # Two sheets bound to page 1: the first, pending, is the one that counts.
    submission, sheets = _submission(db, batch, [pages[0], pages[0]])
    _answer(db, sheets[0], question, ids[0], review_required=True)
    _answer(db, sheets[1], question, ids[0])
    assert _score(db, submission, actor) == SubmissionScoreRead(
        raw_score=0.0, max_possible_score=2.0, percentage=0.0
    )


def test_linked_copies_score_their_first_reading_once(db, actor, batch, pages):
    group = uuid.uuid4()
    first, first_ids = _question(db, pages[0], 2.0, 0.0, group=group, linked=True)
    second, second_ids = _question(db, pages[1], 2.0, 0.0, group=group, linked=True)
    submission, sheets = _submission(db, batch, pages)
    _answer(db, sheets[0], first, first_ids[1])
    _answer(db, sheets[1], second, second_ids[0])
    assert _score(db, submission, actor) == SubmissionScoreRead(
        raw_score=0.0, max_possible_score=2.0, percentage=0.0
    )


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_a_missing_page_is_worth_zero_and_keeps_the_maximum(db, actor, batch, pages):
    on_first, first_ids = _question(db, pages[0], 2.0, 0.0)
    _question(db, pages[1], 2.0, 0.0)
    # Only the sheet of page 1 arrived.
    submission, sheets = _submission(db, batch, pages[:1])
    _answer(db, sheets[0], on_first, first_ids[0])
    assert _score(db, submission, actor) == SubmissionScoreRead(
        raw_score=2.0, max_possible_score=4.0, percentage=50.0
    )


@pytest.mark.parametrize(
    "status", [SubmissionStatus.MATCH_FAILED, SubmissionStatus.UNRESOLVABLE]
)
def test_a_submission_without_a_variant_has_no_score(db, actor, batch, status):
    submission = Submission(submission_batch_id=batch.id, status=status)
    db.add(submission)
    db.flush()
    assert _score(db, submission, actor) is None


@pytest.mark.parametrize(
    "status",
    [SubmissionStatus.UPLOADED, SubmissionStatus.UNASSIGNED, SubmissionStatus.MATCHED],
)
def test_a_submission_not_extracted_has_no_score_even_with_a_variant(
    db, actor, batch, pages, status
):
    """A "0 / N (0%)" before extraction would read as a real grade."""
    _question(db, pages[0], 2.0, 0.0)
    submission, _sheets = _submission(db, batch, pages[:1], status=status)
    assert submission.matched_form_variant_id is not None
    assert _score(db, submission, actor) is None


def test_a_maximum_of_zero_has_no_percentage(db, actor, batch, pages):
    question, ids = _question(db, pages[0], 0.0, 0.0)
    submission, sheets = _submission(db, batch, pages[:1])
    _answer(db, sheets[0], question, ids[0])
    assert _score(db, submission, actor) == SubmissionScoreRead(
        raw_score=0.0, max_possible_score=0.0, percentage=None
    )


def test_a_closed_submission_keeps_its_score(db, actor, batch, pages):
    question, ids = _question(db, pages[0], 2.0, 0.0)
    submission, sheets = _submission(db, batch, pages[:1], status=SubmissionStatus.REVIEWED)
    _answer(db, sheets[0], question, ids[0])
    assert _score(db, submission, actor).raw_score == 2.0


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def test_a_broken_reading_gives_no_score_and_the_read_succeeds(
    db, actor, batch, pages, caplog
):
    question, _ids = _question(db, pages[0], 2.0, 0.0)
    submission, sheets = _submission(db, batch, pages[:1])
    # An option id the question does not have: a broken invariant.
    _answer(db, sheets[0], question, uuid.uuid4())
    with caplog.at_level(logging.ERROR, logger="app.services.submission_score_service"):
        detail = get_submission_review(submission_id=submission.id, db=db, user=actor)
    assert detail.score is None
    assert detail.id == submission.id
    assert str(submission.id) in caplog.text


# ---------------------------------------------------------------------------
# total_score
# ---------------------------------------------------------------------------


def test_the_close_writes_the_score_the_page_was_showing(db, actor, batch, pages):
    first, first_ids = _question(db, pages[0], 2.0, 0.0)
    second, second_ids = _question(db, pages[0], 3.0, 0.0)
    submission, sheets = _submission(db, batch, pages[:1])
    _answer(db, sheets[0], first, first_ids[0])
    # Left pending: worth 0, at the close as before it.
    _answer(db, sheets[0], second, second_ids[0], review_required=True)
    shown = _score(db, submission, actor)
    assert shown.raw_score == 2.0

    detail = _close(db, submission, actor)

    assert detail.status is SubmissionStatus.REVIEWED
    assert detail.score == shown
    assert _total_score(db, submission) == 2.0


def test_the_close_writes_a_negative_score_as_it_is(db, actor, batch, pages):
    question, ids = _question(
        db, pages[0], -1.0, 0.0, 0.0, question_type=QuestionType.MULTIPLE_CHOICE
    )
    submission, sheets = _submission(db, batch, pages[:1])
    _answer(db, sheets[0], question, ids[0])

    _close(db, submission, actor)

    assert _total_score(db, submission) == -1.0


def test_the_close_writes_zero_with_a_maximum_of_zero(db, actor, batch, pages):
    question, ids = _question(db, pages[0], 0.0, 0.0)
    submission, sheets = _submission(db, batch, pages[:1])
    _answer(db, sheets[0], question, ids[0])

    _close(db, submission, actor)

    assert _total_score(db, submission) == 0.0


def test_a_score_that_cannot_be_computed_leaves_total_score_null(
    db, actor, batch, pages, caplog
):
    """The close succeeds; the broken invariant is only logged."""
    question, _ids = _question(db, pages[0], 2.0, 0.0)
    submission, sheets = _submission(db, batch, pages[:1])
    _answer(db, sheets[0], question, uuid.uuid4())

    with caplog.at_level(logging.ERROR, logger="app.services.submission_score_service"):
        detail = _close(db, submission, actor)

    assert detail.status is SubmissionStatus.REVIEWED
    assert detail.score is None
    assert _total_score(db, submission) is None
    assert str(submission.id) in caplog.text


def test_a_reclose_overwrites_total_score(db, actor, batch, pages):
    question, ids = _question(db, pages[0], 2.0, 0.0)
    submission, sheets = _submission(db, batch, pages[:1])
    read = _answer(db, sheets[0], question, ids[1], review_required=True)
    _act(db, submission, read, actor, ReviewActionType.CONFIRM)
    _close(db, submission, actor)
    assert _total_score(db, submission) == 0.0

    _reopen(db, submission, actor)
    assert _total_score(db, submission) is None

    _act(
        db, submission, read, actor, ReviewActionType.SUBSTITUTE,
        {"selected_option_ids": [str(ids[0])]},
    )
    _close(db, submission, actor)
    assert _total_score(db, submission) == 2.0


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


def _count_statements(db, call) -> tuple[int, SubmissionReviewDetail]:
    statements: list[str] = []

    def _record(conn, cursor, statement, *args):
        statements.append(statement)

    engine = db.get_bind().engine
    event.listen(engine, "before_cursor_execute", _record)
    try:
        # A fresh identity map, so nothing a builder left loaded can hide a
        # lazy load the scoring would do in production.
        db.expire_all()
        detail = call()
    finally:
        event.remove(engine, "before_cursor_execute", _record)
    return len(statements), detail


def _scored_submission(db, project, actor, batch, copies: int) -> Submission:
    """A submission of *copies* sheets of valid readings, each with a QUESTION,
    a linked QUESTION and an evaluative MATRIX: the scoring runs to the end, so
    options, cells and the linked group's bindings come from the eager loads,
    never from a lazy load per result. Worth 4 points per sheet, plus 1 for
    the linked group."""
    variant = FormVariant(
        form_project_id=project.id,
        root_key=f"root-{uuid.uuid4().hex[:8]}",
        name=f"V{copies}",
        status=FormVariantStatus.PUBLISHED,
        version_number=copies + 10,
        created_by_user_id=actor.id,
    )
    db.add(variant)
    db.flush()
    template_pages = [
        FormVariantPage(form_variant_id=variant.id, page_index=index, width_px=794, height_px=1123)
        for index in range(1, copies + 1)
    ]
    db.add_all(template_pages)
    db.flush()
    group = uuid.uuid4()
    submission, sheets = _submission(db, batch, template_pages)
    for template_page, sheet in zip(template_pages, sheets):
        question, ids = _question(db, template_page, 2.0, 0.0)
        _answer(db, sheet, question, ids[0])
        linked, linked_ids = _question(db, template_page, 1.0, 0.0, group=group, linked=True)
        _answer(db, sheet, linked, linked_ids[0])
        matrix = _matrix(db, template_page, {(0, 0): 1.0, (1, 1): 1.0})
        db.add(
            SubmissionObjectResult(
                submission_page_id=sheet.id,
                page_object_id=matrix.id,
                result_type=SubmissionResultType.MATRIX,
                detected_payload_json={
                    "rows": [
                        {"row_index": 0, "selected_cells": [{"col_index": 0, "confidence": 0.9}], "validity": "VALID"},
                        {"row_index": 1, "selected_cells": [{"col_index": 1, "confidence": 0.9}], "validity": "VALID"},
                    ]
                },
                confidence_score=0.95,
                validity_forces_review=False,
                review_required=False,
            )
        )
    db.flush()
    return submission


def test_the_statement_count_does_not_grow_with_scored_readings(db, project, actor, batch):
    small = _scored_submission(db, project, actor, batch, 1)
    large = _scored_submission(db, project, actor, batch, 4)

    def _read(submission):
        return lambda: get_submission_review(submission_id=submission.id, db=db, user=actor)

    small_count, small_detail = _count_statements(db, _read(small))
    large_count, large_detail = _count_statements(db, _read(large))

    # Per sheet: 2 + 2 (MATRIX); the linked group once, 1.
    assert small_detail.score == SubmissionScoreRead(raw_score=5.0, max_possible_score=5.0, percentage=100.0)
    assert large_detail.score == SubmissionScoreRead(raw_score=17.0, max_possible_score=17.0, percentage=100.0)
    assert small_count == large_count


def test_the_statement_count_of_a_close_does_not_grow_with_scored_readings(
    db, project, actor, batch
):
    """The score the close writes costs the same statements for 1 and for many
    sheets of valid readings."""
    small = _scored_submission(db, project, actor, batch, 1)
    large = _scored_submission(db, project, actor, batch, 4)

    def _close_at_version_0(submission):
        return lambda: close_submission(
            submission_id=submission.id,
            data=SubmissionCloseRequest(review_version=0),
            db=db,
            user=actor,
        )

    small_count, _ = _count_statements(db, _close_at_version_0(small))
    large_count, _ = _count_statements(db, _close_at_version_0(large))

    assert (_total_score(db, small), _total_score(db, large)) == (5.0, 17.0)
    assert small_count == large_count
