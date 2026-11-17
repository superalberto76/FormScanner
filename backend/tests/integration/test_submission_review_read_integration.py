# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for the submission review page read.

``GET /submissions/{submission_id}/review`` is the one read behind the page
where an operator reviews a submission. What this suite pins down:

  * the header — batch, variant, candidate, missing pages, lock, version and
    the batch's effective threshold;
  * EVERY sheet, in ``(source_page_order, id)`` order, an unplaced one with no
    template page and no objects, and the template page's size (null allowed);
  * only ``review_required`` results are listed, resolved or not, ``resolved``
    being ``review_outcome IS NOT NULL`` (a review log row alone is not
    enough), with the two counts consistent;
  * reading order inside a sheet (y, then x);
  * ``flag_reasons`` per type (the respondent-ID grid's AMBIGUOUS_DIGITS,
    MISSING_DIGITS and UNREADABLE_GRID included), including the empty list of
    a resolved result after the threshold dropped, and never empty for an
    unresolved one;
  * authorisation and status: any status, CLOSED projects, 403 / 404 as the
    other submission routes;
  * the read writes nothing — the lock and the version are left alone;
  * **the query budget**: the same statement count for 1 sheet / 2 results
    and for 4 sheets / 12 results.

Skipped without TEST_DATABASE_URL, like the rest of tests/integration.
"""
from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select

from app.api.v1.endpoints.submissions import get_submission_review
from app.models.enums import (
    FormProjectStatus,
    FormProjectType,
    FormVariantStatus,
    PageObjectRole,
    PageObjectType,
    PageOrderSource,
    ProjectRole,
    QuestionType,
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
from app.models.form_variant_role_binding import FormVariantRoleBinding
from app.models.page_object import PageObject
from app.models.question_object_config import QuestionObjectConfig
from app.models.submission import Submission
from app.models.submission_batch import SubmissionBatch
from app.models.submission_object_result import SubmissionObjectResult
from app.models.submission_page import SubmissionPage
from app.models.submission_review import SubmissionReview
from app.models.user import User
from app.schemas.submission import SubmissionReviewDetail, SubmissionReviewObject
from app.services import submission_lock_service
from app.services.submission_review_service import (
    ALWAYS_REVIEW,
    AMBIGUOUS_DIGITS,
    DECODE_FAILED,
    EXTRACTION_FAILED,
    INVALID_ANSWER,
    LOW_CONFIDENCE,
    MISSING_DIGITS,
    UNREADABLE_GRID,
)

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
THRESHOLD = 0.8

# What respondent_identifier_extraction_service writes for a respondent-ID
# grid with an ambiguous column, with an empty column, with both, and for the
# degenerate grid.
AMBIGUOUS_GRID = {
    "raw_value": "12345",
    "digits": [],
    "ambiguous_columns": [2],
}
MISSING_DIGIT_GRID = {"raw_value": "1234?", "digits": [], "ambiguous_columns": []}
AMBIGUOUS_AND_MISSING_GRID = {
    "raw_value": "12?45",
    "digits": [],
    "ambiguous_columns": [0],
}
DEGENERATE_GRID = {"raw_value": "", "digits": [], "ambiguous_columns": []}
# Passed as ``payload`` to build a "Review anyway" placeholder (NULL payload).
PLACEHOLDER: dict = {}


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_user(db, first: str | None = None, last: str | None = None) -> User:
    u = User(
        email=f"review-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.USER,
        first_name=first,
        last_name=last,
    )
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def frozen_now(monkeypatch) -> datetime:
    monkeypatch.setattr(submission_lock_service, "_now", lambda: NOW)
    return NOW


@pytest.fixture
def actor(db) -> User:
    return _make_user(db, "Mario", "Rossi")


@pytest.fixture
def project(db, actor) -> FormProject:
    project = FormProject(
        owner_id=actor.id,
        type=FormProjectType.EXAM,
        name="Review project",
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
def batch(db, project, actor) -> SubmissionBatch:
    batch = SubmissionBatch(
        form_project_id=project.id,
        name="Morning batch",
        created_by_user_id=actor.id,
        confidence_threshold=THRESHOLD,
    )
    db.add(batch)
    db.flush()
    return batch


def _make_variant(
    db, project, actor, *, pages: int = 2, sized: bool = True
) -> tuple[FormVariant, list[FormVariantPage]]:
    """A PUBLISHED variant with *pages* template pages, indexed 1..pages;
    A4 at 96 DPI when *sized*, null dimensions otherwise."""
    version = 1 + db.scalar(
        select(func.count(FormVariant.id)).where(
            FormVariant.form_project_id == project.id
        )
    )
    variant = FormVariant(
        form_project_id=project.id,
        root_key=f"root-{uuid.uuid4().hex[:8]}",
        name=f"Variant {version}",
        status=FormVariantStatus.PUBLISHED,
        version_number=version,
        created_by_user_id=actor.id,
    )
    db.add(variant)
    db.flush()
    template_pages = []
    for index in range(1, pages + 1):
        page = FormVariantPage(
            form_variant_id=variant.id,
            page_index=index,
            width_px=794 if sized else None,
            height_px=1123 if sized else None,
        )
        db.add(page)
        template_pages.append(page)
    db.flush()
    return variant, template_pages


def _make_submission(
    db,
    batch,
    variant: FormVariant | None,
    bound_to: list[FormVariantPage | None],
    *,
    status: SubmissionStatus = SubmissionStatus.SCANNED,
    respondent_identifier: str | None = None,
) -> tuple[Submission, list[SubmissionPage]]:
    """A submission with one sheet per entry of *bound_to* (None: unplaced)."""
    submission = Submission(
        submission_batch_id=batch.id,
        matched_form_variant_id=variant.id if variant is not None else None,
        status=status,
        respondent_identifier=respondent_identifier,
    )
    db.add(submission)
    db.flush()
    sheets = []
    for order, template_page in enumerate(bound_to):
        sheet = SubmissionPage(
            submission_id=submission.id,
            source_file_path=f"/tmp/{uuid.uuid4().hex}.png",
            source_filename=f"page{order}.png",
            source_page_order=order,
            page_order_source=PageOrderSource.CODE,
            form_variant_page_id=template_page.id if template_page else None,
            match_confidence=1.0 if template_page else 0.0,
        )
        db.add(sheet)
        sheets.append(sheet)
    db.flush()
    return submission, sheets


_OBJECT_TYPE = {
    SubmissionResultType.QUESTION: PageObjectType.QUESTION,
    SubmissionResultType.MATRIX: PageObjectType.MATRIX,
    SubmissionResultType.FREE_TEXT: PageObjectType.FREE_TEXT_AREA,
    SubmissionResultType.CODE: PageObjectType.CODE,
    SubmissionResultType.RESPONDENT_IDENTIFIER: PageObjectType.MATRIX,
}


def _make_result(
    db,
    sheet: SubmissionPage,
    result_type: SubmissionResultType = SubmissionResultType.QUESTION,
    *,
    confidence: float | None = 0.5,
    forces: bool = False,
    review_required: bool | None = None,
    x: float = 10,
    y: float = 10,
    label: str | None = None,
    question_text: str = "How are you?",
    role: PageObjectRole | None = None,
    payload: dict | None = None,
) -> SubmissionObjectResult:
    """An authored object on *sheet*'s template page and its reading.

    ``review_required`` defaults to what the extractors would write under
    ``THRESHOLD``."""
    obj = PageObject(
        form_variant_page_id=sheet.form_variant_page_id,
        object_type=_OBJECT_TYPE[result_type],
        x=x,
        y=y,
        width=100,
        height=40,
        label=label,
    )
    db.add(obj)
    db.flush()
    if result_type is SubmissionResultType.QUESTION:
        db.add(
            QuestionObjectConfig(
                page_object_id=obj.id,
                question_type=QuestionType.SINGLE_CHOICE,
                question_text=question_text,
            )
        )
    if role is not None:
        variant_id = db.get(FormVariantPage, sheet.form_variant_page_id).form_variant_id
        db.add(
            FormVariantRoleBinding(
                form_variant_id=variant_id, page_object_id=obj.id, role=role
            )
        )
    if review_required is None:
        review_required = forces or (confidence is not None and confidence < THRESHOLD)
    result = SubmissionObjectResult(
        submission_page_id=sheet.id,
        page_object_id=obj.id,
        result_type=result_type,
        detected_payload_json=(
            None if payload is PLACEHOLDER
            else payload if payload is not None
            else {"probe": str(obj.id)}
        ),
        confidence_score=confidence,
        validity_forces_review=forces,
        review_required=review_required,
    )
    db.add(result)
    db.flush()
    return result


def _review(
    db,
    result: SubmissionObjectResult,
    reviewer: User,
    outcome: ReviewOutcome = ReviewOutcome.CONFIRMED,
) -> None:
    """A ruling as ``review_action_service`` writes it: the log row and the
    current state on the result."""
    db.add(
        SubmissionReview(
            submission_object_result_id=result.id,
            reviewer_user_id=reviewer.id,
            action=ReviewAction(outcome.value),
        )
    )
    result.review_outcome = outcome
    result.reviewed_by = reviewer.id
    result.reviewed_at = NOW
    db.flush()


def _read(db, submission, user) -> SubmissionReviewDetail:
    return get_submission_review(submission_id=submission.id, db=db, user=user)


def _objects(detail: SubmissionReviewDetail) -> dict[uuid.UUID, SubmissionReviewObject]:
    return {o.result_id: o for p in detail.pages for o in p.objects}


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_the_header(db, project, actor, batch, frozen_now):
    variant, (p1, _p2) = _make_variant(db, project, actor)
    submission, _ = _make_submission(
        db, batch, variant, [p1], respondent_identifier="123456"
    )

    detail = _read(db, submission, actor)

    assert detail.id == submission.id
    assert detail.submission_batch_id == batch.id
    assert detail.batch_name == "Morning batch"
    assert detail.form_project_id == project.id
    assert detail.status is SubmissionStatus.SCANNED
    assert detail.created_at == submission.created_at
    assert detail.matched_form_variant_id == variant.id
    assert detail.variant_name == variant.name
    assert detail.respondent_identifier == "123456"
    assert detail.respondent_identifier_expected is False
    assert detail.missing_page_indices == [2]
    assert detail.lock is None
    assert detail.review_version == 0
    assert detail.effective_threshold == THRESHOLD
    assert detail.review_count == 0
    assert detail.pending_review_count == 0


def test_a_candidate_is_expected_when_the_variant_binds_the_role(
    db, project, actor, batch, frozen_now
):
    variant, (p1, _p2) = _make_variant(db, project, actor)
    submission, (sheet,) = _make_submission(db, batch, variant, [p1])
    _make_result(
        db,
        sheet,
        SubmissionResultType.RESPONDENT_IDENTIFIER,
        confidence=0.95,
        role=PageObjectRole.RESPONDENT_IDENTIFIER,
    )

    assert _read(db, submission, actor).respondent_identifier_expected is True


def test_no_variant(db, project, actor, batch, frozen_now):
    submission, _ = _make_submission(
        db, batch, None, [None], status=SubmissionStatus.MATCH_FAILED
    )

    detail = _read(db, submission, actor)

    assert detail.matched_form_variant_id is None
    assert detail.variant_name is None
    assert detail.missing_page_indices == []
    assert detail.respondent_identifier_expected is False
    assert [p.template_page for p in detail.pages] == [None]


def test_every_sheet_is_listed_in_order_including_an_unplaced_one(
    db, project, actor, batch, frozen_now
):
    variant, (p1, p2) = _make_variant(db, project, actor)
    submission, sheets = _make_submission(db, batch, variant, [p2, None, p1])
    # A sheet declaring the same position as another: the id breaks the tie.
    twin = SubmissionPage(
        submission_id=submission.id,
        source_file_path="/tmp/twin.png",
        source_filename="twin.png",
        source_page_order=0,
        form_variant_page_id=None,
    )
    db.add(twin)
    db.flush()
    _make_result(db, sheets[0], confidence=0.5)

    detail = _read(db, submission, actor)

    first_two = sorted([sheets[0].id, twin.id])
    assert [p.id for p in detail.pages] == [*first_two, sheets[1].id, sheets[2].id]
    by_id = {p.id: p for p in detail.pages}
    unplaced = by_id[sheets[1].id]
    assert unplaced.template_page is None
    assert unplaced.objects == []
    assert unplaced.source_page_order == 1
    assert unplaced.page_order_source is PageOrderSource.CODE
    placed = by_id[sheets[0].id].template_page
    assert placed is not None
    assert (placed.id, placed.page_index) == (p2.id, 2)
    assert (placed.width_px, placed.height_px) == (794, 1123)
    assert detail.missing_page_indices == []


def test_template_page_dimensions_may_be_null(db, project, actor, batch, frozen_now):
    variant, (p1,) = _make_variant(db, project, actor, pages=1, sized=False)
    submission, _ = _make_submission(db, batch, variant, [p1])

    template = _read(db, submission, actor).pages[0].template_page

    assert template is not None
    assert (template.width_px, template.height_px) == (None, None)


def test_object_fields(db, project, actor, batch, frozen_now):
    variant, (p1,) = _make_variant(db, project, actor, pages=1)
    submission, (sheet,) = _make_submission(db, batch, variant, [p1])
    question = _make_result(
        db, sheet, confidence=0.5, x=12.5, y=30, label="Q1", question_text="Pick one"
    )
    code = _make_result(
        db, sheet, SubmissionResultType.CODE, confidence=None, forces=True, y=300
    )

    objects = _objects(_read(db, submission, actor))

    q = objects[question.id]
    assert q.page_object_id == question.page_object_id
    assert q.object_type is PageObjectType.QUESTION
    assert q.result_type is SubmissionResultType.QUESTION
    assert q.role is None
    assert q.label == "Q1"
    assert q.question_text == "Pick one"
    assert q.geometry.model_dump() == {"x": 12.5, "y": 30, "width": 100, "height": 40}
    assert q.detected_payload == {"probe": str(question.page_object_id)}
    assert q.confidence_score == 0.5
    c = objects[code.id]
    assert c.object_type is PageObjectType.CODE
    assert c.question_text is None
    assert c.confidence_score is None


def test_role_is_reported(db, project, actor, batch, frozen_now):
    variant, (p1,) = _make_variant(db, project, actor, pages=1)
    submission, (sheet,) = _make_submission(db, batch, variant, [p1])
    rid = _make_result(
        db,
        sheet,
        SubmissionResultType.RESPONDENT_IDENTIFIER,
        confidence=0.4,
        role=PageObjectRole.RESPONDENT_IDENTIFIER,
    )

    obj = _objects(_read(db, submission, actor))[rid.id]

    assert obj.role is PageObjectRole.RESPONDENT_IDENTIFIER
    assert obj.question_text is None
    assert obj.flag_reasons == [LOW_CONFIDENCE]


# ---------------------------------------------------------------------------
# Which results, resolved, counts, order
# ---------------------------------------------------------------------------


def test_only_review_required_results_are_listed(db, project, actor, batch, frozen_now):
    variant, (p1,) = _make_variant(db, project, actor, pages=1)
    submission, (sheet,) = _make_submission(db, batch, variant, [p1])
    flagged = _make_result(db, sheet, confidence=0.5)
    accepted = _make_result(db, sheet, confidence=0.99, y=200)
    assert accepted.review_required is False

    detail = _read(db, submission, actor)

    assert set(_objects(detail)) == {flagged.id}
    assert detail.review_count == 1


def test_resolved_follows_the_review_outcome_and_counts_agree(
    db, project, actor, batch, frozen_now
):
    variant, (p1, p2) = _make_variant(db, project, actor)
    submission, (s1, s2) = _make_submission(db, batch, variant, [p1, p2])
    a = _make_result(db, s1, confidence=0.5)
    b = _make_result(db, s1, confidence=0.4, y=200)
    c = _make_result(db, s2, SubmissionResultType.FREE_TEXT, confidence=0.9, forces=True)
    _review(db, b, actor)
    # The unused ``reviewed`` column must NOT count as resolved…
    a.reviewed = True
    # …nor a review LOG row with no current ruling (UNDONE).
    db.add(
        SubmissionReview(
            submission_object_result_id=c.id,
            reviewer_user_id=actor.id,
            action=ReviewAction.UNDONE,
        )
    )
    db.flush()

    detail = _read(db, submission, actor)
    objects = _objects(detail)

    assert objects[a.id].resolved is False
    assert objects[b.id].resolved is True
    assert objects[c.id].resolved is False
    assert detail.review_count == 3
    assert detail.pending_review_count == 2
    assert detail.pending_review_count == sum(not o.resolved for o in objects.values())


def test_reading_order_is_y_then_x(db, project, actor, batch, frozen_now):
    variant, (p1,) = _make_variant(db, project, actor, pages=1)
    submission, (sheet,) = _make_submission(db, batch, variant, [p1])
    bottom = _make_result(db, sheet, y=500, x=10)
    top_right = _make_result(db, sheet, y=50, x=400)
    top_left = _make_result(db, sheet, y=50, x=20)
    middle = _make_result(db, sheet, y=200, x=900)

    objects = _read(db, submission, actor).pages[0].objects

    assert [o.result_id for o in objects] == [
        top_left.id,
        top_right.id,
        middle.id,
        bottom.id,
    ]


# ---------------------------------------------------------------------------
# flag_reasons
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "result_type, confidence, forces, payload, expected",
    [
        (SubmissionResultType.QUESTION, 0.5, False, None, [LOW_CONFIDENCE]),
        (SubmissionResultType.QUESTION, 0.95, True, None, [INVALID_ANSWER]),
        (SubmissionResultType.QUESTION, 0.5, True, None, [LOW_CONFIDENCE, INVALID_ANSWER]),
        (SubmissionResultType.MATRIX, 0.95, True, None, [INVALID_ANSWER]),
        (SubmissionResultType.MATRIX, 0.3, False, None, [LOW_CONFIDENCE]),
        (SubmissionResultType.FREE_TEXT, 0.95, True, None, [ALWAYS_REVIEW]),
        (SubmissionResultType.FREE_TEXT, None, True, None, [ALWAYS_REVIEW]),
        (SubmissionResultType.FREE_TEXT, 0.2, True, None, [LOW_CONFIDENCE, ALWAYS_REVIEW]),
        (SubmissionResultType.CODE, None, True, None, [DECODE_FAILED]),
        (SubmissionResultType.RESPONDENT_IDENTIFIER, 0.6, False, None, [LOW_CONFIDENCE]),
        (
            SubmissionResultType.RESPONDENT_IDENTIFIER,
            0.95,
            True,
            AMBIGUOUS_GRID,
            [AMBIGUOUS_DIGITS],
        ),
        (
            SubmissionResultType.RESPONDENT_IDENTIFIER,
            0.5,
            True,
            AMBIGUOUS_GRID,
            [LOW_CONFIDENCE, AMBIGUOUS_DIGITS],
        ),
        (
            SubmissionResultType.RESPONDENT_IDENTIFIER,
            None,
            True,
            DEGENERATE_GRID,
            [UNREADABLE_GRID],
        ),
        (
            SubmissionResultType.RESPONDENT_IDENTIFIER,
            0.95,
            True,
            MISSING_DIGIT_GRID,
            [MISSING_DIGITS],
        ),
        (
            SubmissionResultType.RESPONDENT_IDENTIFIER,
            0.5,
            True,
            MISSING_DIGIT_GRID,
            [LOW_CONFIDENCE, MISSING_DIGITS],
        ),
        (
            SubmissionResultType.RESPONDENT_IDENTIFIER,
            0.95,
            True,
            AMBIGUOUS_AND_MISSING_GRID,
            [AMBIGUOUS_DIGITS, MISSING_DIGITS],
        ),
    ],
    ids=[
        "question-low-confidence",
        "question-invalid",
        "question-both",
        "matrix-invalid",
        "matrix-low-confidence",
        "free-text",
        "free-text-no-ocr",
        "free-text-low-confidence",
        "code-not-decoded",
        "respondent-identifier-low-confidence",
        "respondent-identifier-ambiguous",
        "respondent-identifier-low-confidence-and-ambiguous",
        "respondent-identifier-unreadable-grid",
        "respondent-identifier-missing-digits",
        "respondent-identifier-low-confidence-and-missing-digits",
        "respondent-identifier-ambiguous-and-missing-digits",
    ],
)
def test_flag_reasons(
    db,
    project,
    actor,
    batch,
    frozen_now,
    result_type,
    confidence,
    forces,
    payload,
    expected,
):
    variant, (p1,) = _make_variant(db, project, actor, pages=1)
    submission, (sheet,) = _make_submission(db, batch, variant, [p1])
    result = _make_result(
        db, sheet, result_type, confidence=confidence, forces=forces, payload=payload
    )

    obj = _objects(_read(db, submission, actor))[result.id]

    assert obj.flag_reasons == expected
    # Unresolved: never without a reason.
    assert obj.resolved is False and obj.flag_reasons


@pytest.mark.parametrize("result_type", list(SubmissionResultType))
def test_a_placeholder_is_extraction_failed_alone(
    db, project, actor, batch, frozen_now, result_type
):
    """A "Review anyway" placeholder forces review like an invalid reading, but
    must never be labelled one: EXTRACTION_FAILED comes first, and alone."""
    variant, (p1,) = _make_variant(db, project, actor, pages=1)
    submission, (sheet,) = _make_submission(db, batch, variant, [p1])
    role = (
        PageObjectRole.RESPONDENT_IDENTIFIER
        if result_type is SubmissionResultType.RESPONDENT_IDENTIFIER
        else None
    )
    result = _make_result(
        db, sheet, result_type, confidence=None, forces=True, payload=PLACEHOLDER, role=role
    )

    obj = _objects(_read(db, submission, actor))[result.id]

    assert obj.flag_reasons == [EXTRACTION_FAILED]
    assert obj.detected_payload is None
    assert obj.resolved is False


def test_a_confidence_equal_to_the_threshold_is_not_low(
    db, project, actor, batch, frozen_now
):
    variant, (p1,) = _make_variant(db, project, actor, pages=1)
    submission, (sheet,) = _make_submission(db, batch, variant, [p1])
    result = _make_result(db, sheet, confidence=THRESHOLD, forces=True)

    assert _objects(_read(db, submission, actor))[result.id].flag_reasons == [
        INVALID_ANSWER
    ]


def test_a_resolved_result_after_the_threshold_dropped_has_no_reason(
    db, project, actor, batch, frozen_now
):
    variant, (p1,) = _make_variant(db, project, actor, pages=1)
    submission, (sheet,) = _make_submission(db, batch, variant, [p1])
    result = _make_result(db, sheet, confidence=0.5)
    _review(db, result, actor)
    # review_required stays frozen at True; the threshold no longer flags it.
    batch.confidence_threshold = 0.3
    db.flush()

    detail = _read(db, submission, actor)
    obj = _objects(detail)[result.id]

    assert detail.effective_threshold == 0.3
    assert obj.resolved is True
    assert obj.flag_reasons == []


def test_unresolved_results_always_carry_a_reason(db, project, actor, batch, frozen_now):
    variant, (p1, p2) = _make_variant(db, project, actor)
    submission, (s1, s2) = _make_submission(db, batch, variant, [p1, p2])
    # Every way each extractor can send a reading to review.
    cases = [
        (SubmissionResultType.QUESTION, 0.1, False, None),
        (SubmissionResultType.QUESTION, 0.9, True, None),
        (SubmissionResultType.MATRIX, 0.9, True, None),
        (SubmissionResultType.MATRIX, 0.79, False, None),
        (SubmissionResultType.FREE_TEXT, None, True, None),
        (SubmissionResultType.FREE_TEXT, 0.99, True, None),
        (SubmissionResultType.CODE, None, True, None),
        (SubmissionResultType.RESPONDENT_IDENTIFIER, 0.0, False, None),
        (SubmissionResultType.RESPONDENT_IDENTIFIER, 0.99, True, AMBIGUOUS_GRID),
        (SubmissionResultType.RESPONDENT_IDENTIFIER, None, True, DEGENERATE_GRID),
        (SubmissionResultType.RESPONDENT_IDENTIFIER, 0.99, True, MISSING_DIGIT_GRID),
    ]
    for i, (result_type, confidence, forces, payload) in enumerate(cases):
        _make_result(
            db, s1 if i % 2 else s2, result_type,
            confidence=confidence, forces=forces, y=10 * i, payload=payload,
        )

    objects = list(_objects(_read(db, submission, actor)).values())

    assert len(objects) == len(cases)
    assert {o.result_type for o in objects} == set(SubmissionResultType)
    assert all(not o.resolved for o in objects)
    assert [o for o in objects if not o.flag_reasons] == []


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------


def test_an_active_lock_is_reported_with_its_holder(
    db, project, actor, batch, frozen_now
):
    luca = _make_user(db, "Luca", "Bianchi")
    submission, _ = _make_submission(db, batch, None, [None])
    submission.locked_by = luca.id
    submission.locked_at = NOW - timedelta(minutes=5)
    db.flush()

    lock = _read(db, submission, actor).lock

    assert lock is not None
    assert lock.user_id == luca.id
    assert lock.display_name == "Luca Bianchi"
    assert lock.locked_at == NOW - timedelta(minutes=5)


def test_an_expired_lock_is_reported_as_none(db, project, actor, batch, frozen_now):
    luca = _make_user(db, "Luca", "Bianchi")
    submission, _ = _make_submission(db, batch, None, [None])
    submission.locked_by = luca.id
    submission.locked_at = NOW - submission_lock_service.LOCK_TIMEOUT
    db.flush()

    assert _read(db, submission, actor).lock is None


# ---------------------------------------------------------------------------
# Status, authorisation, no writes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "submission_status",
    [SubmissionStatus.REVIEWED, SubmissionStatus.FINALIZED, SubmissionStatus.MATCHED],
)
def test_any_status_can_be_read(
    db, project, actor, batch, frozen_now, submission_status
):
    variant, (p1,) = _make_variant(db, project, actor, pages=1)
    submission, (sheet,) = _make_submission(
        db, batch, variant, [p1], status=submission_status
    )
    _make_result(db, sheet, confidence=0.5)

    detail = _read(db, submission, actor)

    assert detail.status is submission_status
    assert detail.review_count == 1


@pytest.mark.parametrize(
    "project_status", [FormProjectStatus.CLOSED, FormProjectStatus.ARCHIVED]
)
def test_a_closed_project_can_be_read(
    db, project, actor, batch, frozen_now, project_status
):
    project.status = project_status
    submission, _ = _make_submission(db, batch, None, [None])
    db.flush()

    assert _read(db, submission, actor).id == submission.id


def test_a_non_member_gets_the_usual_403(db, project, actor, batch, frozen_now):
    submission, _ = _make_submission(db, batch, None, [None])
    outsider = _make_user(db, "Out", "Sider")

    with pytest.raises(HTTPException) as excinfo:
        _read(db, submission, outsider)

    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "You are not a member of this project"


def test_an_unknown_submission_is_404(db, actor, frozen_now):
    with pytest.raises(HTTPException) as excinfo:
        get_submission_review(submission_id=uuid.uuid4(), db=db, user=actor)

    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "Submission not found"


def test_reading_writes_nothing(db, project, actor, batch, frozen_now):
    luca = _make_user(db, "Luca", "Bianchi")
    variant, (p1,) = _make_variant(db, project, actor, pages=1)
    submission, (sheet,) = _make_submission(db, batch, variant, [p1])
    _make_result(db, sheet, confidence=0.5)
    # Someone else's EXPIRED lock: an acquire would overwrite it, a read must not.
    submission.locked_by = luca.id
    submission.locked_at = NOW - timedelta(hours=1)
    submission.review_version = 3
    db.flush()
    db.refresh(submission)
    before = (
        submission.locked_by,
        submission.locked_at,
        submission.review_version,
        submission.updated_at,
    )

    statements: list[str] = []

    def _record(conn, cursor, statement, *args):
        statements.append(statement)

    engine = db.get_bind().engine
    event.listen(engine, "before_cursor_execute", _record)
    try:
        detail = _read(db, submission, actor)
        db.flush()
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert detail.review_version == 3
    assert detail.lock is None
    assert not [s for s in statements if not s.lstrip().upper().startswith("SELECT")]
    db.expire_all()
    after = db.get(Submission, submission.id)
    assert (
        after.locked_by,
        after.locked_at,
        after.review_version,
        after.updated_at,
    ) == before


# ---------------------------------------------------------------------------
# Query budget
# ---------------------------------------------------------------------------


def _count_statements(db, submission, actor) -> tuple[int, SubmissionReviewDetail]:
    statements: list[str] = []

    def _record(conn, cursor, statement, *args):
        statements.append(statement)

    engine = db.get_bind().engine
    event.listen(engine, "before_cursor_execute", _record)
    try:
        # A fresh identity map, so nothing a builder left loaded can hide a
        # lazy load the endpoint would do in production.
        db.expire_all()
        detail = _read(db, submission, actor)
    finally:
        event.remove(engine, "before_cursor_execute", _record)
    return len(statements), detail


@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "locked"])
def test_the_statement_count_does_not_grow_with_sheets_and_results(
    db, project, actor, batch, frozen_now, locked
):
    """1 sheet / 2 results or 4 sheets / 12 results, the same statements: the
    sheets, results, objects, roles, question configs and reviewers all come
    from one query each, never from a per-row lazy load."""
    luca = _make_user(db, "Luca", "Bianchi")

    def _build_submission(sheets: int, per_sheet: int) -> Submission:
        variant, template_pages = _make_variant(db, project, actor, pages=sheets)
        submission, rows = _make_submission(db, batch, variant, template_pages)
        rid_bound = False
        turns = itertools.count()
        for sheet in rows:
            for i in range(per_sheet):
                if not rid_bound:
                    _make_result(
                        db,
                        sheet,
                        SubmissionResultType.RESPONDENT_IDENTIFIER,
                        confidence=0.4,
                        role=PageObjectRole.RESPONDENT_IDENTIFIER,
                        y=i,
                    )
                    rid_bound = True
                    continue
                result = _make_result(db, sheet, confidence=0.5, y=10 * i)
                if i % 2:
                    # Rulings by two different people, with different
                    # outcomes: the reviewers still come from ONE query.
                    alternate = next(turns) % 2
                    reviewer = actor if alternate else luca
                    outcome = ReviewOutcome.SUBSTITUTED if alternate else ReviewOutcome.FLAGGED
                    _review(db, result, reviewer, outcome)
        if locked:
            submission.locked_by = luca.id
            submission.locked_at = NOW - timedelta(minutes=1)
            db.flush()
        return submission

    small = _build_submission(1, 2)
    large = _build_submission(4, 3)

    small_count, small_detail = _count_statements(db, small, actor)
    large_count, large_detail = _count_statements(db, large, actor)

    assert (small_detail.review_count, len(small_detail.pages)) == (2, 1)
    assert (large_detail.review_count, len(large_detail.pages)) == (12, 4)
    assert (small_detail.lock is not None) is locked
    reviewers = {
        o.reviewed_by.display_name
        for o in (o for p in large_detail.pages for o in p.objects)
        if o.reviewed_by is not None
    }
    assert reviewers == {"Mario Rossi", "Luca Bianchi"}
    assert small_count == large_count
