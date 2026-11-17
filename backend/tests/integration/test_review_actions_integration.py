# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for the review actions.

``POST /submissions/{id}/results/{result_id}/review`` (CONFIRM, SUBSTITUTE,
FLAG, UNDO) and ``POST /submissions/{id}/accept-remaining``. What this suite
pins down:

  * **every action on every result type** — what it leaves on the result (the
    current ruling) and the append-only log row it writes, note included;
  * **CONFIRM refusals** — 409 ``NOT_CONFIRMABLE`` with its ``reason`` for a
    placeholder, a respondent ID with ``?`` or empty, an undecoded CODE and a
    free-text identifier with an empty suggestion;
  * **SUBSTITUTE values** — valid and invalid per type, validity computed by
    the server for QUESTION (single, multiple) and MATRIX (row and column
    rules), an empty selection meaning NO_ANSWER;
  * **UNDO** — refused on an unresolved result, clearing everything otherwise;
    overwriting a resolved result; the log keeps every step;
  * **the candidate** — CONFIRM / SUBSTITUTE of a respondent-ID grid or of a
    free-text identifier set it, FLAG / UNDO never touch it;
  * **the guard** — LOCK_LOST, SUBMISSION_CHANGED and NOT_REVIEWABLE on every
    action and on accept-remaining, and a result of another submission is 404;
    a refusal writes nothing;
  * **accept-remaining** — exactly the bulk-acceptable unresolved results, ONE
    version bump, one audit event, zero eligible still a 200;
  * **the read** — the new per-object fields, ``confirmable`` /
    ``bulk_acceptable`` per case, ``bulk_acceptable_count``;
  * **statement counts** — a save and an accept-remaining cost the same
    however many results the submission holds.

Skipped without TEST_DATABASE_URL, like the rest of tests/integration.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import event, func, select

from app.api.v1.endpoints.submissions import (
    accept_remaining_submission_results,
    get_submission_review,
    review_submission_result,
)
from app.models.audit_log import AuditLog
from app.models.enums import (
    FormProjectStatus,
    FormProjectType,
    FormVariantStatus,
    MatrixSelectionRule,
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
from app.models.free_text_area_config import FreeTextAreaConfig
from app.models.matrix_object_config import MatrixObjectConfig
from app.models.page_object import PageObject
from app.models.question_object_config import QuestionObjectConfig
from app.models.question_option import QuestionOption
from app.models.submission import Submission
from app.models.submission_batch import SubmissionBatch
from app.models.submission_object_result import SubmissionObjectResult
from app.models.submission_page import SubmissionPage
from app.models.submission_review import SubmissionReview
from app.models.user import User
from app.schemas.submission import (
    SubmissionAcceptRemainingRequest,
    SubmissionReviewActionRequest,
    SubmissionReviewDetail,
    SubmissionReviewObject,
)
from app.services import (
    audit_service,
    review_action_service,
    review_rules,
    submission_lock_service,
)

NOW = datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc)
THRESHOLD = 0.8

# Kinds of reading this suite builds (see ``_make``).
QUESTION = "question"
QUESTION_MULTI = "question_multi"
MATRIX_ROWS = "matrix_rows"
MATRIX_COLS = "matrix_cols"
FREE_TEXT = "free_text"
CODE = "code"
RID_GRID = "rid_grid"
RID_TEXT = "rid_text"

CLEAN_GRID = {"raw_value": "12345", "digits": [], "ambiguous_columns": []}
AMBIGUOUS_GRID = {"raw_value": "12345", "digits": [], "ambiguous_columns": [2]}
PLACEHOLDER: dict = {}


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_user(db, first: str, last: str) -> User:
    u = User(
        email=f"actions-{uuid.uuid4().hex}@example.com",
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
    monkeypatch.setattr(review_action_service, "_now", lambda: NOW)
    return NOW


@pytest.fixture
def ticking(monkeypatch) -> None:
    """Every ruling one second after the previous one, so log rows written in
    the one transaction a test runs in still have a meaningful order (each
    row's ``created_at`` is the action's instant)."""
    ticks = iter(range(10_000))
    monkeypatch.setattr(submission_lock_service, "_now", lambda: NOW)
    monkeypatch.setattr(
        review_action_service, "_now", lambda: NOW + timedelta(seconds=next(ticks))
    )


@pytest.fixture
def actor(db) -> User:
    return _make_user(db, "Mario", "Rossi")


def _member(db, project, user) -> User:
    db.add(FormProjectMember(project_id=project.id, user_id=user.id, role=ProjectRole.COLLABORATOR))
    db.flush()
    return user


@pytest.fixture
def project(db, actor) -> FormProject:
    project = FormProject(
        owner_id=actor.id,
        type=FormProjectType.EXAM,
        name="Actions project",
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
        name="Batch",
        created_by_user_id=actor.id,
        confidence_threshold=THRESHOLD,
    )
    db.add(batch)
    db.flush()
    return batch


@pytest.fixture
def template_page(db, project, actor) -> FormVariantPage:
    variant = FormVariant(
        form_project_id=project.id,
        root_key=f"root-{uuid.uuid4().hex[:8]}",
        name="Variant",
        status=FormVariantStatus.PUBLISHED,
        version_number=1,
        created_by_user_id=actor.id,
    )
    db.add(variant)
    db.flush()
    page = FormVariantPage(form_variant_id=variant.id, page_index=1, width_px=794, height_px=1123)
    db.add(page)
    db.flush()
    return page


def _make_submission(
    db, batch, template_page, *, status=SubmissionStatus.SCANNED, candidate=None
) -> tuple[Submission, SubmissionPage]:
    submission = Submission(
        submission_batch_id=batch.id,
        matched_form_variant_id=template_page.form_variant_id,
        status=status,
        respondent_identifier=candidate,
    )
    db.add(submission)
    db.flush()
    sheet = SubmissionPage(
        submission_id=submission.id,
        source_file_path=f"/tmp/{uuid.uuid4().hex}.png",
        source_filename="page.png",
        source_page_order=0,
        page_order_source=PageOrderSource.CODE,
        form_variant_page_id=template_page.id,
        match_confidence=1.0,
    )
    db.add(sheet)
    db.flush()
    return submission, sheet


@pytest.fixture
def submission_and_sheet(db, batch, template_page) -> tuple[Submission, SubmissionPage]:
    return _make_submission(db, batch, template_page, candidate="ORIGINAL")


_RESULT_TYPES = {
    QUESTION: SubmissionResultType.QUESTION,
    QUESTION_MULTI: SubmissionResultType.QUESTION,
    MATRIX_ROWS: SubmissionResultType.MATRIX,
    MATRIX_COLS: SubmissionResultType.MATRIX,
    FREE_TEXT: SubmissionResultType.FREE_TEXT,
    CODE: SubmissionResultType.CODE,
    RID_GRID: SubmissionResultType.RESPONDENT_IDENTIFIER,
    RID_TEXT: SubmissionResultType.FREE_TEXT,
}
_OBJECT_TYPES = {
    QUESTION: PageObjectType.QUESTION,
    QUESTION_MULTI: PageObjectType.QUESTION,
    MATRIX_ROWS: PageObjectType.MATRIX,
    MATRIX_COLS: PageObjectType.MATRIX,
    FREE_TEXT: PageObjectType.FREE_TEXT_AREA,
    CODE: PageObjectType.CODE,
    RID_GRID: PageObjectType.MATRIX,
    RID_TEXT: PageObjectType.FREE_TEXT_AREA,
}
# What each extractor would plausibly write, when the test does not care.
_DEFAULT_PAYLOADS = {
    QUESTION: {"selected_options": [], "validity": "NO_ANSWER"},
    QUESTION_MULTI: {"selected_options": [], "validity": "NO_ANSWER"},
    MATRIX_ROWS: {"rows": [], "cols": []},
    MATRIX_COLS: {"rows": [], "cols": []},
    FREE_TEXT: {"suggested_text": "Some words"},
    CODE: {"decoded_value": "ABC", "decode_successful": True},
    RID_GRID: CLEAN_GRID,
    RID_TEXT: {"suggested_text": "  654321  "},
}


def _make(
    db,
    sheet: SubmissionPage,
    kind: str,
    *,
    payload: dict | None = None,
    confidence: float | None = 0.5,
    forces: bool = False,
    review_required: bool = True,
    y: float = 10,
) -> SubmissionObjectResult:
    """An authored object of *kind* on *sheet*'s template page, with its config
    (3 options; a 2x3 matrix), its role when it has one, and its reading.

    A variant binds RESPONDENT_IDENTIFIER to ONE object, so only the first
    identifier object of a variant gets the binding. That matters for a
    free-text identifier (the role is what makes it one) and not for a grid
    (its ``result_type`` is): build the free-text one first when a test needs
    both."""
    obj = PageObject(
        form_variant_page_id=sheet.form_variant_page_id,
        object_type=_OBJECT_TYPES[kind],
        x=10, y=y, width=200, height=80,
    )
    db.add(obj)
    db.flush()
    if kind in (QUESTION, QUESTION_MULTI):
        config = QuestionObjectConfig(
            page_object_id=obj.id,
            question_type=(
                QuestionType.MULTIPLE_CHOICE if kind == QUESTION_MULTI else QuestionType.SINGLE_CHOICE
            ),
            question_text="Pick",
        )
        db.add(config)
        db.flush()
        for i in range(3):
            db.add(QuestionOption(question_object_config_id=config.id, order_index=i, option_text=f"O{i}"))
    if kind in (MATRIX_ROWS, MATRIX_COLS, RID_GRID):
        db.add(
            MatrixObjectConfig(
                page_object_id=obj.id,
                rows_count=2,
                cols_count=3,
                selection_rule=(
                    MatrixSelectionRule.ONE_PER_COLUMN if kind == MATRIX_COLS else MatrixSelectionRule.ONE_PER_ROW
                ),
            )
        )
    if kind in (FREE_TEXT, RID_TEXT):
        db.add(FreeTextAreaConfig(page_object_id=obj.id))
    variant_id = db.get(FormVariantPage, sheet.form_variant_page_id).form_variant_id
    already_bound = db.scalar(
        select(func.count(FormVariantRoleBinding.id)).where(
            FormVariantRoleBinding.form_variant_id == variant_id,
            FormVariantRoleBinding.role == PageObjectRole.RESPONDENT_IDENTIFIER,
        )
    )
    if kind in (RID_GRID, RID_TEXT) and not already_bound:
        db.add(
            FormVariantRoleBinding(
                form_variant_id=variant_id,
                page_object_id=obj.id,
                role=PageObjectRole.RESPONDENT_IDENTIFIER,
            )
        )
    result = SubmissionObjectResult(
        submission_page_id=sheet.id,
        page_object_id=obj.id,
        result_type=_RESULT_TYPES[kind],
        detected_payload_json=(
            None if payload is PLACEHOLDER else payload if payload is not None else _DEFAULT_PAYLOADS[kind]
        ),
        confidence_score=confidence,
        validity_forces_review=forces or kind in (FREE_TEXT, RID_TEXT) or payload is PLACEHOLDER,
        review_required=review_required,
    )
    db.add(result)
    db.flush()
    db.refresh(obj)
    return result


def _option_ids(db, result: SubmissionObjectResult) -> list[uuid.UUID]:
    config = db.scalar(
        select(QuestionObjectConfig).where(QuestionObjectConfig.page_object_id == result.page_object_id)
    )
    return [o.id for o in sorted(config.options, key=lambda o: o.order_index)]


def _act(
    db,
    submission: Submission,
    result: SubmissionObjectResult,
    action: str,
    user: User,
    *,
    version: int | None = None,
    note: str | None = None,
    value: dict | None = None,
) -> SubmissionReviewDetail:
    return review_submission_result(
        submission_id=submission.id,
        result_id=result.id,
        data=SubmissionReviewActionRequest(
            action=action,
            review_version=submission.review_version if version is None else version,
            note=note,
            value=value,
        ),
        db=db,
        user=user,
    )


def _accept(db, submission, user, *, version: int | None = None):
    return accept_remaining_submission_results(
        submission_id=submission.id,
        data=SubmissionAcceptRemainingRequest(
            review_version=submission.review_version if version is None else version
        ),
        db=db,
        user=user,
    )


def _refused(db, call) -> HTTPException:
    """Run a call expected to fail; roll back as ``get_db`` would on error.

    The test's own setup is committed first (to the per-test SAVEPOINT, see
    conftest), so the rollback discards exactly what the refused call wrote —
    which is what the "a refusal writes nothing" assertions check."""
    db.commit()
    with pytest.raises(HTTPException) as excinfo:
        call()
    db.rollback()
    return excinfo.value


def _log(db, result) -> list[SubmissionReview]:
    db.expire_all()
    return list(
        db.scalars(
            select(SubmissionReview)
            .where(SubmissionReview.submission_object_result_id == result.id)
            .order_by(SubmissionReview.created_at, SubmissionReview.id)
        ).all()
    )


def _objects(detail: SubmissionReviewDetail) -> dict[uuid.UUID, SubmissionReviewObject]:
    return {o.result_id: o for p in detail.pages for o in p.objects}


def _fresh(db, model, id_):
    db.expire_all()
    return db.get(model, id_)


# ---------------------------------------------------------------------------
# CONFIRM
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", [QUESTION, MATRIX_ROWS, FREE_TEXT, CODE, RID_GRID, RID_TEXT])
def test_confirm_records_the_ruling_and_a_log_row(db, actor, submission_and_sheet, frozen_now, kind):
    submission, sheet = submission_and_sheet
    result = _make(db, sheet, kind)

    detail = _act(db, submission, result, "CONFIRM", actor, note="  Looks right  ")

    stored = _fresh(db, SubmissionObjectResult, result.id)
    assert stored.review_outcome is ReviewOutcome.CONFIRMED
    assert stored.reviewed_payload_json is None
    assert stored.review_note == "Looks right"
    assert stored.reviewed_by == actor.id
    assert stored.reviewed_at == NOW
    (row,) = _log(db, result)
    assert row.action is ReviewAction.CONFIRMED
    assert row.review_note == "Looks right"
    assert row.reviewer_user_id == actor.id
    assert row.reviewed_payload_json is None
    obj = _objects(detail)[result.id]
    assert obj.resolved is True and obj.review_outcome is ReviewOutcome.CONFIRMED


@pytest.mark.parametrize(
    "kind, payload, reason",
    [
        (QUESTION, PLACEHOLDER, review_rules.NO_READING),
        (MATRIX_ROWS, PLACEHOLDER, review_rules.NO_READING),
        (FREE_TEXT, PLACEHOLDER, review_rules.NO_READING),
        (CODE, PLACEHOLDER, review_rules.NO_READING),
        (RID_GRID, PLACEHOLDER, review_rules.NO_READING),
        (RID_TEXT, PLACEHOLDER, review_rules.NO_READING),
        (RID_GRID, {"raw_value": "12?45", "digits": [], "ambiguous_columns": []}, review_rules.INCOMPLETE_IDENTIFIER),
        (RID_GRID, {"raw_value": "", "digits": [], "ambiguous_columns": []}, review_rules.EMPTY_IDENTIFIER),
        (CODE, {"decoded_value": None, "decode_successful": False}, review_rules.NOT_DECODED),
        (RID_TEXT, {"suggested_text": "   "}, review_rules.EMPTY_IDENTIFIER),
        (RID_TEXT, {"suggested_text": "9" * 501}, review_rules.IDENTIFIER_TOO_LONG),
    ],
    ids=[
        "placeholder-question", "placeholder-matrix", "placeholder-free-text",
        "placeholder-code", "placeholder-grid", "placeholder-identifier-text",
        "grid-with-question-mark", "grid-empty", "code-not-decoded",
        "identifier-text-empty", "identifier-text-too-long",
    ],
)
def test_confirm_of_an_incomplete_reading_is_refused(
    db, actor, submission_and_sheet, frozen_now, kind, payload, reason
):
    submission, sheet = submission_and_sheet
    result = _make(db, sheet, kind, payload=payload)

    exc = _refused(db, lambda: _act(db, submission, result, "CONFIRM", actor, version=0))

    assert exc.status_code == 409
    assert exc.detail["code"] == review_action_service.NOT_CONFIRMABLE
    assert exc.detail["reason"] == reason
    stored = _fresh(db, SubmissionObjectResult, result.id)
    assert stored.review_outcome is None
    assert _log(db, result) == []
    after = _fresh(db, Submission, submission.id)
    assert after.review_version == 0
    assert after.respondent_identifier == "ORIGINAL"


def test_confirm_takes_no_value(db, actor, submission_and_sheet, frozen_now):
    submission, sheet = submission_and_sheet
    result = _make(db, sheet, CODE)

    exc = _refused(db, lambda: _act(db, submission, result, "CONFIRM", actor, value={"value": "x"}))

    assert exc.status_code == 422
    assert _fresh(db, SubmissionObjectResult, result.id).review_outcome is None


# ---------------------------------------------------------------------------
# SUBSTITUTE — QUESTION
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind, picks, validity",
    [
        (QUESTION, [1], "VALID"),
        (QUESTION, [2, 0], "TOO_MANY_ANSWERS"),
        (QUESTION, [], "NO_ANSWER"),
        (QUESTION_MULTI, [2, 0], "VALID"),
        (QUESTION_MULTI, [], "NO_ANSWER"),
    ],
    ids=["single-one", "single-two", "single-none", "multi-two", "multi-none"],
)
def test_substitute_question_computes_validity_on_the_server(
    db, actor, submission_and_sheet, frozen_now, kind, picks, validity
):
    submission, sheet = submission_and_sheet
    result = _make(db, sheet, kind)
    options = _option_ids(db, result)

    detail = _act(
        db, submission, result, "SUBSTITUTE", actor,
        value={"selected_option_ids": [str(options[i]) for i in picks]},
    )

    expected = {
        # In the question's option order, whatever order they were sent in.
        "selected_option_ids": [str(options[i]) for i in sorted(picks)],
        "validity": validity,
    }
    stored = _fresh(db, SubmissionObjectResult, result.id)
    assert stored.review_outcome is ReviewOutcome.SUBSTITUTED
    assert stored.reviewed_payload_json == expected
    (row,) = _log(db, result)
    assert row.action is ReviewAction.SUBSTITUTED
    assert row.reviewed_payload_json == expected
    assert _objects(detail)[result.id].reviewed_payload == expected


def test_substitute_question_rejects_bad_values(db, actor, submission_and_sheet, frozen_now):
    submission, sheet = submission_and_sheet
    result = _make(db, sheet, QUESTION)
    other = _make(db, sheet, QUESTION, y=200)
    mine, foreign = _option_ids(db, result)[0], _option_ids(db, other)[0]

    for value in (
        {"selected_option_ids": [str(foreign)]},          # another question's option
        {"selected_option_ids": [str(mine), str(mine)]},  # duplicate
        {"selected_option_ids": ["not-a-uuid"]},
        {"text": "hello"},                                # another type's shape
        {"selected_option_ids": [], "extra": 1},
        None,                                             # missing
    ):
        exc = _refused(db, lambda: _act(db, submission, result, "SUBSTITUTE", actor, version=0, value=value))
        assert exc.status_code == 422, value
    assert _fresh(db, SubmissionObjectResult, result.id).review_outcome is None
    assert _fresh(db, Submission, submission.id).review_version == 0


# ---------------------------------------------------------------------------
# SUBSTITUTE — MATRIX
# ---------------------------------------------------------------------------


def test_substitute_matrix_under_a_row_rule_lists_every_row(db, actor, submission_and_sheet, frozen_now):
    submission, sheet = submission_and_sheet
    result = _make(db, sheet, MATRIX_ROWS)

    _act(db, submission, result, "SUBSTITUTE", actor, value={"selected_cells": [[0, 2], [0, 1]]})

    assert _fresh(db, SubmissionObjectResult, result.id).reviewed_payload_json == {
        "rows": [
            {"row_index": 0, "selected_col_indices": [1, 2], "validity": "TOO_MANY_ANSWERS"},
            {"row_index": 1, "selected_col_indices": [], "validity": "NO_ANSWER"},
        ],
        "cols": [],
    }


def test_substitute_matrix_under_a_column_rule_groups_by_column(db, actor, submission_and_sheet, frozen_now):
    submission, sheet = submission_and_sheet
    result = _make(db, sheet, MATRIX_COLS)

    _act(db, submission, result, "SUBSTITUTE", actor, value={"selected_cells": [[1, 0], [0, 2]]})

    assert _fresh(db, SubmissionObjectResult, result.id).reviewed_payload_json == {
        "rows": [],
        "cols": [
            {"col_index": 0, "selected_row_indices": [1], "validity": "VALID"},
            {"col_index": 1, "selected_row_indices": [], "validity": "NO_ANSWER"},
            {"col_index": 2, "selected_row_indices": [0], "validity": "VALID"},
        ],
    }


def test_substitute_matrix_with_nothing_selected_is_no_answer(db, actor, submission_and_sheet, frozen_now):
    submission, sheet = submission_and_sheet
    result = _make(db, sheet, MATRIX_ROWS)

    _act(db, submission, result, "SUBSTITUTE", actor, value={"selected_cells": []})

    payload = _fresh(db, SubmissionObjectResult, result.id).reviewed_payload_json
    assert [r["validity"] for r in payload["rows"]] == ["NO_ANSWER", "NO_ANSWER"]


def test_substitute_matrix_rejects_bad_values(db, actor, submission_and_sheet, frozen_now):
    submission, sheet = submission_and_sheet
    result = _make(db, sheet, MATRIX_ROWS)

    for value in (
        {"selected_cells": [[2, 0]]},           # row out of range (2 rows)
        {"selected_cells": [[0, 3]]},           # column out of range (3 cols)
        {"selected_cells": [[-1, 0]]},
        {"selected_cells": [[0, 1], [0, 1]]},   # duplicate
        {"selected_cells": [[0]]},              # not a pair
        {"selected_option_ids": []},            # another type's shape
    ):
        exc = _refused(db, lambda: _act(db, submission, result, "SUBSTITUTE", actor, version=0, value=value))
        assert exc.status_code == 422, value
    assert _fresh(db, SubmissionObjectResult, result.id).review_outcome is None


# ---------------------------------------------------------------------------
# SUBSTITUTE — identifier, free text, code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", [RID_GRID, RID_TEXT])
def test_substitute_identifier_sets_the_candidate(db, actor, submission_and_sheet, frozen_now, kind):
    submission, sheet = submission_and_sheet
    result = _make(db, sheet, kind)

    detail = _act(db, submission, result, "SUBSTITUTE", actor, value={"value": "  98765  "})

    assert _fresh(db, SubmissionObjectResult, result.id).reviewed_payload_json == {"value": "98765"}
    assert _fresh(db, Submission, submission.id).respondent_identifier == "98765"
    assert detail.respondent_identifier == "98765"


@pytest.mark.parametrize("kind", [RID_GRID, RID_TEXT])
def test_substitute_identifier_rejects_bad_values(db, actor, submission_and_sheet, frozen_now, kind):
    submission, sheet = submission_and_sheet
    result = _make(db, sheet, kind)

    for value in ({"value": "   "}, {"value": "1" * 501}, {"text": "123"}):
        exc = _refused(db, lambda: _act(db, submission, result, "SUBSTITUTE", actor, version=0, value=value))
        assert exc.status_code == 422, value
    # 500 exactly, once trimmed, is fine.
    _act(db, submission, result, "SUBSTITUTE", actor, version=0, value={"value": " " + "1" * 500 + " "})
    assert _fresh(db, Submission, submission.id).respondent_identifier == "1" * 500


def test_substitute_free_text(db, actor, submission_and_sheet, frozen_now):
    submission, sheet = submission_and_sheet
    result = _make(db, sheet, FREE_TEXT)
    other = _make(db, sheet, FREE_TEXT, y=300)

    _act(db, submission, result, "SUBSTITUTE", actor, value={"text": "  The answer  "})
    # Empty is allowed: the candidate wrote nothing.
    _act(db, submission, other, "SUBSTITUTE", actor, value={"text": "   "})

    assert _fresh(db, SubmissionObjectResult, result.id).reviewed_payload_json == {"text": "The answer"}
    assert _fresh(db, SubmissionObjectResult, other.id).reviewed_payload_json == {"text": ""}
    # A free-text ANSWER never touches the candidate.
    assert _fresh(db, Submission, submission.id).respondent_identifier == "ORIGINAL"


def test_substitute_free_text_rejects_bad_values(db, actor, submission_and_sheet, frozen_now):
    submission, sheet = submission_and_sheet
    result = _make(db, sheet, FREE_TEXT)

    for value in ({"text": "x" * 5001}, {"value": "x"}, {"text": None}):
        exc = _refused(db, lambda: _act(db, submission, result, "SUBSTITUTE", actor, version=0, value=value))
        assert exc.status_code == 422, value
    _act(db, submission, result, "SUBSTITUTE", actor, version=0, value={"text": "x" * 5000})
    assert len(_fresh(db, SubmissionObjectResult, result.id).reviewed_payload_json["text"]) == 5000


def test_substitute_code(db, actor, submission_and_sheet, frozen_now):
    submission, sheet = submission_and_sheet
    result = _make(db, sheet, CODE, payload={"decoded_value": None, "decode_successful": False})

    for value in ({"value": "  "}, {"value": "c" * 2001}, {"text": "c"}):
        exc = _refused(db, lambda: _act(db, submission, result, "SUBSTITUTE", actor, version=0, value=value))
        assert exc.status_code == 422, value

    _act(db, submission, result, "SUBSTITUTE", actor, version=0, value={"value": "  QR-42 "})
    assert _fresh(db, SubmissionObjectResult, result.id).reviewed_payload_json == {"value": "QR-42"}


@pytest.mark.parametrize(
    "kind, value, expected",
    [
        (MATRIX_ROWS, {"selected_cells": []}, {"rows": [
            {"row_index": 0, "selected_col_indices": [], "validity": "NO_ANSWER"},
            {"row_index": 1, "selected_col_indices": [], "validity": "NO_ANSWER"},
        ], "cols": []}),
        (CODE, {"value": "X"}, {"value": "X"}),
        (FREE_TEXT, {"text": "words"}, {"text": "words"}),
        (RID_TEXT, {"value": "777"}, {"value": "777"}),
    ],
)
def test_a_placeholder_is_substituted_in_the_shape_of_its_type(
    db, actor, submission_and_sheet, frozen_now, kind, value, expected
):
    submission, sheet = submission_and_sheet
    result = _make(db, sheet, kind, payload=PLACEHOLDER)

    _act(db, submission, result, "SUBSTITUTE", actor, value=value)

    assert _fresh(db, SubmissionObjectResult, result.id).reviewed_payload_json == expected


def test_a_question_placeholder_is_substituted_with_its_options(db, actor, submission_and_sheet, frozen_now):
    submission, sheet = submission_and_sheet
    result = _make(db, sheet, QUESTION, payload=PLACEHOLDER)
    option = _option_ids(db, result)[1]

    _act(db, submission, result, "SUBSTITUTE", actor, value={"selected_option_ids": [str(option)]})

    assert _fresh(db, SubmissionObjectResult, result.id).reviewed_payload_json == {
        "selected_option_ids": [str(option)], "validity": "VALID",
    }


# ---------------------------------------------------------------------------
# FLAG, UNDO, overwrite, the log
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", [QUESTION, CODE, RID_GRID])
def test_flag(db, actor, submission_and_sheet, frozen_now, kind):
    submission, sheet = submission_and_sheet
    # Flagging needs no complete reading: a placeholder can be flagged.
    result = _make(db, sheet, kind, payload=PLACEHOLDER)

    _act(db, submission, result, "FLAG", actor, note="Torn corner")

    stored = _fresh(db, SubmissionObjectResult, result.id)
    assert stored.review_outcome is ReviewOutcome.FLAGGED
    assert stored.reviewed_payload_json is None
    assert stored.review_note == "Torn corner"
    (row,) = _log(db, result)
    assert (row.action, row.review_note, row.reviewed_payload_json) == (
        ReviewAction.FLAGGED, "Torn corner", None,
    )
    assert _fresh(db, Submission, submission.id).respondent_identifier == "ORIGINAL"


def test_undo_of_an_unresolved_result_is_refused(db, actor, submission_and_sheet, frozen_now):
    submission, sheet = submission_and_sheet
    result = _make(db, sheet, QUESTION)

    exc = _refused(db, lambda: _act(db, submission, result, "UNDO", actor))

    assert exc.status_code == 409
    assert exc.detail["code"] == review_action_service.NOT_RESOLVED
    assert _log(db, result) == []
    assert _fresh(db, Submission, submission.id).review_version == 0


def test_undo_clears_the_ruling_and_logs_it(db, actor, submission_and_sheet, ticking):
    submission, sheet = submission_and_sheet
    result = _make(db, sheet, RID_GRID)
    _act(db, submission, result, "SUBSTITUTE", actor, value={"value": "55555"}, note="fixed")
    submission = _fresh(db, Submission, submission.id)

    detail = _act(db, submission, result, "UNDO", actor, note="  changed my mind ")

    stored = _fresh(db, SubmissionObjectResult, result.id)
    assert (
        stored.review_outcome, stored.reviewed_payload_json, stored.review_note,
        stored.reviewed_by, stored.reviewed_at,
    ) == (None, None, None, None, None)
    actions = [(r.action, r.review_note) for r in _log(db, result)]
    assert actions == [
        (ReviewAction.SUBSTITUTED, "fixed"),
        (ReviewAction.UNDONE, "changed my mind"),
    ]
    # The candidate keeps what the SUBSTITUTE set: UNDO never touches it.
    assert _fresh(db, Submission, submission.id).respondent_identifier == "55555"
    obj = _objects(detail)[result.id]
    assert obj.resolved is False and obj.review_outcome is None
    assert detail.pending_review_count == 1


def test_a_resolved_result_is_overwritten_and_the_log_keeps_every_step(
    db, actor, project, submission_and_sheet, ticking
):
    luca = _member(db, project, _make_user(db, "Luca", "Bianchi"))
    submission, sheet = submission_and_sheet
    result = _make(db, sheet, QUESTION)
    option = _option_ids(db, result)[0]

    _act(db, submission, result, "CONFIRM", actor, note="ok")
    # Each colleague takes the lock over before saving, as the FE would.
    submission_lock_service.take_over(db, submission.id, luca)
    _act(db, _fresh(db, Submission, submission.id), result, "SUBSTITUTE", luca,
         value={"selected_option_ids": [str(option)]})
    submission_lock_service.take_over(db, submission.id, actor)
    _act(db, _fresh(db, Submission, submission.id), result, "FLAG", actor, note="unsure")

    stored = _fresh(db, SubmissionObjectResult, result.id)
    assert stored.review_outcome is ReviewOutcome.FLAGGED
    assert stored.reviewed_payload_json is None
    assert stored.review_note == "unsure"
    assert stored.reviewed_by == actor.id
    assert stored.reviewed_at == NOW + timedelta(seconds=2)
    log = _log(db, result)
    assert [(r.action, r.reviewer_user_id) for r in log] == [
        (ReviewAction.CONFIRMED, actor.id),
        (ReviewAction.SUBSTITUTED, luca.id),
        (ReviewAction.FLAGGED, actor.id),
    ]
    assert log[1].reviewed_payload_json == {
        "selected_option_ids": [str(option)], "validity": "VALID",
    }
    assert _fresh(db, Submission, submission.id).review_version == 3


def test_a_blank_note_is_null_and_a_long_one_is_refused():
    assert SubmissionReviewActionRequest(action="FLAG", review_version=0, note="   ").note is None
    assert SubmissionReviewActionRequest(action="FLAG", review_version=0, note="n" * 2000).note == "n" * 2000
    assert SubmissionReviewActionRequest(
        action="FLAG", review_version=0, note="  " + "n" * 2000 + "  "
    ).note == "n" * 2000
    with pytest.raises(ValidationError):
        SubmissionReviewActionRequest(action="FLAG", review_version=0, note="n" * 2001)
    with pytest.raises(ValidationError):
        SubmissionReviewActionRequest(action="DELETE", review_version=0)


# ---------------------------------------------------------------------------
# The candidate
# ---------------------------------------------------------------------------


def test_confirm_of_a_respondent_id_grid_sets_the_candidate(db, actor, submission_and_sheet, frozen_now):
    submission, sheet = submission_and_sheet
    result = _make(db, sheet, RID_GRID, payload={"raw_value": "24680", "digits": [], "ambiguous_columns": []})

    detail = _act(db, submission, result, "CONFIRM", actor)

    assert _fresh(db, Submission, submission.id).respondent_identifier == "24680"
    assert detail.respondent_identifier == "24680"


def test_confirm_of_a_free_text_identifier_sets_the_trimmed_suggestion(db, actor, submission_and_sheet, frozen_now):
    submission, sheet = submission_and_sheet
    result = _make(db, sheet, RID_TEXT, payload={"suggested_text": "  AB 123  "})

    _act(db, submission, result, "CONFIRM", actor)

    assert _fresh(db, Submission, submission.id).respondent_identifier == "AB 123"


@pytest.mark.parametrize("kind", [RID_GRID, RID_TEXT])
def test_flag_and_undo_never_touch_the_candidate(db, actor, submission_and_sheet, frozen_now, kind):
    submission, sheet = submission_and_sheet
    result = _make(db, sheet, kind)

    _act(db, submission, result, "FLAG", actor)
    assert _fresh(db, Submission, submission.id).respondent_identifier == "ORIGINAL"
    _act(db, _fresh(db, Submission, submission.id), result, "UNDO", actor)
    assert _fresh(db, Submission, submission.id).respondent_identifier == "ORIGINAL"


def test_confirm_of_an_answer_never_touches_the_candidate(db, actor, submission_and_sheet, frozen_now):
    submission, sheet = submission_and_sheet
    for i, kind in enumerate((QUESTION, FREE_TEXT, CODE)):
        result = _make(db, sheet, kind, y=100 * i)
        _act(db, _fresh(db, Submission, submission.id), result, "CONFIRM", actor)

    assert _fresh(db, Submission, submission.id).respondent_identifier == "ORIGINAL"


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------

_ACTIONS = ["CONFIRM", "SUBSTITUTE", "FLAG", "UNDO", "ACCEPT_REMAINING"]


def _attempt(db, submission, result, action, user, version=None):
    if action == "ACCEPT_REMAINING":
        return _accept(db, submission, user, version=version)
    value = {"value": "X"} if action == "SUBSTITUTE" else None
    return _act(db, submission, result, action, user, version=version, value=value)


def _resolved_code(db, actor, sheet) -> SubmissionObjectResult:
    # Resolved so UNDO would be admissible; every action applies to a CODE.
    result = _make(db, sheet, CODE)
    result.review_outcome = ReviewOutcome.CONFIRMED
    result.reviewed_by = actor.id
    result.reviewed_at = NOW
    db.flush()
    return result


@pytest.mark.parametrize("action", _ACTIONS)
def test_lock_lost(db, actor, submission_and_sheet, frozen_now, action):
    submission, sheet = submission_and_sheet
    result = _resolved_code(db, actor, sheet)
    luca = _make_user(db, "Luca", "Bianchi")
    submission.locked_by = luca.id
    submission.locked_at = NOW - timedelta(minutes=1)
    db.flush()

    exc = _refused(db, lambda: _attempt(db, submission, result, action, actor, version=0))

    assert exc.status_code == 409
    assert exc.detail["code"] == submission_lock_service.LOCK_LOST
    assert exc.detail["locked_by"]["display_name"] == "Luca Bianchi"
    assert _log(db, result)[0:] == []
    assert _fresh(db, Submission, submission.id).review_version == 0


@pytest.mark.parametrize("action", _ACTIONS)
def test_submission_changed(db, actor, submission_and_sheet, frozen_now, action):
    submission, sheet = submission_and_sheet
    result = _resolved_code(db, actor, sheet)
    submission.review_version = 4
    db.flush()

    exc = _refused(db, lambda: _attempt(db, submission, result, action, actor, version=3))

    assert exc.status_code == 409
    assert exc.detail["code"] == submission_lock_service.SUBMISSION_CHANGED
    assert exc.detail["current_version"] == 4
    assert _fresh(db, SubmissionObjectResult, result.id).review_outcome is ReviewOutcome.CONFIRMED


@pytest.mark.parametrize("action", _ACTIONS)
@pytest.mark.parametrize("status", [SubmissionStatus.MATCHED, SubmissionStatus.REVIEWED])
def test_not_reviewable(db, actor, batch, template_page, frozen_now, action, status):
    submission, sheet = _make_submission(db, batch, template_page, status=status)
    result = _resolved_code(db, actor, sheet)

    exc = _refused(db, lambda: _attempt(db, submission, result, action, actor, version=0))

    assert exc.status_code == 409
    assert exc.detail["code"] == submission_lock_service.NOT_REVIEWABLE
    assert _log(db, result) == []


@pytest.mark.parametrize("action", ["CONFIRM", "SUBSTITUTE", "FLAG", "UNDO"])
def test_a_result_of_another_submission_is_404(db, actor, batch, template_page, submission_and_sheet, frozen_now, action):
    submission, _sheet = submission_and_sheet
    _other, other_sheet = _make_submission(db, batch, template_page)
    foreign = _resolved_code(db, actor, other_sheet)

    exc = _refused(db, lambda: _attempt(db, submission, foreign, action, actor))

    assert exc.status_code == 404
    assert exc.detail == "Result not found"
    assert _fresh(db, Submission, submission.id).review_version == 0
    assert _fresh(db, SubmissionObjectResult, foreign.id).review_outcome is ReviewOutcome.CONFIRMED


def test_a_save_takes_the_lock_and_bumps_the_version(db, actor, submission_and_sheet, frozen_now):
    submission, sheet = submission_and_sheet
    result = _make(db, sheet, CODE)
    luca = _make_user(db, "Luca", "Bianchi")
    # Luca's EXPIRED lock: the save takes it.
    submission.locked_by = luca.id
    submission.locked_at = NOW - timedelta(hours=1)
    db.flush()

    detail = _act(db, submission, result, "CONFIRM", actor)

    assert detail.review_version == 1
    assert detail.lock is not None and detail.lock.user_id == actor.id
    assert detail.lock.locked_at == NOW


def test_single_actions_write_no_audit_event(db, actor, submission_and_sheet, frozen_now):
    submission, sheet = submission_and_sheet
    result = _make(db, sheet, CODE)

    _act(db, submission, result, "CONFIRM", actor)

    assert db.scalar(
        select(func.count(AuditLog.id)).where(AuditLog.entity_id == str(submission.id))
    ) == 0


# ---------------------------------------------------------------------------
# Accept all remaining
# ---------------------------------------------------------------------------


def test_accept_remaining_confirms_exactly_the_bulk_acceptable_unresolved(
    db, actor, submission_and_sheet, frozen_now
):
    submission, sheet = submission_and_sheet
    # The excluded identity readings sit on a later (duplicate) sheet: only the
    # first identity reading in physical order sets the candidate, and the
    # eligible grid must be that one.
    later_sheet = SubmissionPage(
        submission_id=submission.id,
        source_file_path=f"/tmp/{uuid.uuid4().hex}.png",
        source_filename="page-again.png",
        source_page_order=1,
        page_order_source=PageOrderSource.CODE,
        form_variant_page_id=sheet.form_variant_page_id,
        match_confidence=1.0,
    )
    db.add(later_sheet)
    db.flush()
    # First, so it holds the variant's one RESPONDENT_IDENTIFIER binding (the
    # binding is on the object, whatever sheet its reading is on).
    rid_text = _make(db, later_sheet, RID_TEXT, y=11)                         # free-text identifier
    eligible = [
        _make(db, sheet, QUESTION, confidence=0.4, y=1),                      # low confidence
        _make(db, sheet, MATRIX_ROWS, confidence=0.95, forces=True, y=2),     # invalid combination
        _make(db, sheet, CODE, confidence=None, y=3),                         # decoded, flagged anyway
        _make(db, sheet, RID_GRID, payload={"raw_value": "13579", "digits": [], "ambiguous_columns": []},
              confidence=0.5, y=4),                                           # clean grid, low confidence
    ]
    excluded = [
        _make(db, sheet, FREE_TEXT, y=10),                                    # free text
        rid_text,
        _make(db, later_sheet, RID_GRID, payload=AMBIGUOUS_GRID, forces=True, y=12),  # ambiguous digits
        _make(db, later_sheet, RID_GRID, payload={"raw_value": "", "digits": [], "ambiguous_columns": []},
              forces=True, y=13),                                             # unreadable grid
        _make(db, later_sheet, RID_GRID, payload={"raw_value": "1?3", "digits": [], "ambiguous_columns": []},
              y=14),                                                          # incomplete
        _make(db, sheet, CODE, payload={"decoded_value": None, "decode_successful": False},
              forces=True, y=15),                                             # not decoded
        _make(db, sheet, QUESTION, payload=PLACEHOLDER, y=16),                # placeholder
    ]
    already = _make(db, sheet, QUESTION, y=20)
    already.review_outcome = ReviewOutcome.FLAGGED
    already.review_note = "kept"
    already.reviewed_by = actor.id
    already.reviewed_at = NOW - timedelta(days=1)
    not_in_review = _make(db, sheet, QUESTION, confidence=0.99, review_required=False, y=21)
    db.flush()

    response = _accept(db, submission, actor)

    assert response.accepted_count == len(eligible)
    for result in eligible:
        stored = _fresh(db, SubmissionObjectResult, result.id)
        assert stored.review_outcome is ReviewOutcome.CONFIRMED
        assert (stored.reviewed_by, stored.reviewed_at, stored.review_note) == (actor.id, NOW, None)
        (row,) = _log(db, result)
        assert (row.action, row.reviewer_user_id, row.review_note, row.reviewed_payload_json) == (
            ReviewAction.CONFIRMED, actor.id, None, None,
        )
    for result in excluded + [not_in_review]:
        assert _fresh(db, SubmissionObjectResult, result.id).review_outcome is None
        assert _log(db, result) == []
    kept = _fresh(db, SubmissionObjectResult, already.id)
    assert (kept.review_outcome, kept.review_note) == (ReviewOutcome.FLAGGED, "kept")
    after = _fresh(db, Submission, submission.id)
    assert after.review_version == 1  # ONE bump
    assert after.respondent_identifier == "13579"  # the confirmed grid
    assert response.review_version == 1
    assert response.respondent_identifier == "13579"
    assert response.bulk_acceptable_count == 0
    assert response.pending_review_count == len(excluded)


def test_accept_remaining_logs_one_audit_event(db, actor, submission_and_sheet, frozen_now):
    submission, sheet = submission_and_sheet
    _make(db, sheet, QUESTION, y=1)
    _make(db, sheet, CODE, y=2)

    _accept(db, submission, actor)

    events = db.scalars(
        select(AuditLog).where(
            AuditLog.action == audit_service.SUBMISSION_ACCEPTED_REMAINING,
            AuditLog.entity_id == str(submission.id),
        )
    ).all()
    assert len(events) == 1
    assert events[0].metadata_json == {"accepted_count": 2}
    assert events[0].actor_user_id == actor.id


def test_accept_remaining_with_nothing_eligible_still_saves(db, actor, submission_and_sheet, frozen_now):
    submission, sheet = submission_and_sheet
    free_text = _make(db, sheet, FREE_TEXT)

    response = _accept(db, submission, actor)

    assert response.accepted_count == 0
    assert response.review_version == 1
    assert _fresh(db, SubmissionObjectResult, free_text.id).review_outcome is None
    events = db.scalars(
        select(AuditLog).where(AuditLog.action == audit_service.SUBMISSION_ACCEPTED_REMAINING)
        .where(AuditLog.entity_id == str(submission.id))
    ).all()
    assert [e.metadata_json for e in events] == [{"accepted_count": 0}]


def test_accept_remaining_reconfirms_an_undone_result(db, actor, submission_and_sheet, ticking):
    submission, sheet = submission_and_sheet
    result = _make(db, sheet, QUESTION)
    _act(db, submission, result, "FLAG", actor)
    _act(db, _fresh(db, Submission, submission.id), result, "UNDO", actor)

    response = _accept(db, _fresh(db, Submission, submission.id), actor)

    assert response.accepted_count == 1
    assert [r.action for r in _log(db, result)] == [
        ReviewAction.FLAGGED, ReviewAction.UNDONE, ReviewAction.CONFIRMED,
    ]


# ---------------------------------------------------------------------------
# The read
# ---------------------------------------------------------------------------


def test_the_read_reports_the_current_ruling(db, actor, project, submission_and_sheet, frozen_now):
    luca = _member(db, project, _make_user(db, "Luca", "Bianchi"))
    submission, sheet = submission_and_sheet
    substituted = _make(db, sheet, CODE, y=1)
    untouched = _make(db, sheet, QUESTION, y=2)
    _act(db, submission, substituted, "SUBSTITUTE", luca, value={"value": "Z9"}, note="misread")

    detail = get_submission_review(submission_id=submission.id, db=db, user=actor)
    objects = _objects(detail)

    s = objects[substituted.id]
    assert s.resolved is True
    assert s.review_outcome is ReviewOutcome.SUBSTITUTED
    assert s.reviewed_payload == {"value": "Z9"}
    assert s.review_note == "misread"
    assert s.reviewed_by is not None
    assert (s.reviewed_by.user_id, s.reviewed_by.display_name) == (luca.id, "Luca Bianchi")
    assert s.reviewed_at == NOW
    u = objects[untouched.id]
    assert (u.resolved, u.review_outcome, u.reviewed_payload, u.review_note, u.reviewed_by, u.reviewed_at) == (
        False, None, None, None, None, None,
    )
    assert detail.pending_review_count == 1


@pytest.mark.parametrize(
    "kind, payload, forces, confirmable, bulk",
    [
        (QUESTION, None, False, True, True),
        (QUESTION, None, True, True, True),
        (QUESTION, PLACEHOLDER, True, False, False),
        (MATRIX_ROWS, None, True, True, True),
        (FREE_TEXT, None, True, True, False),
        (FREE_TEXT, {"suggested_text": ""}, True, True, False),
        (CODE, None, False, True, True),
        (CODE, {"decoded_value": None, "decode_successful": False}, True, False, False),
        (RID_GRID, CLEAN_GRID, False, True, True),
        (RID_GRID, AMBIGUOUS_GRID, True, True, False),
        (RID_GRID, {"raw_value": "", "digits": [], "ambiguous_columns": []}, True, False, False),
        (RID_GRID, {"raw_value": "1?3", "digits": [], "ambiguous_columns": []}, False, False, False),
        (RID_TEXT, {"suggested_text": "123"}, True, True, False),
        (RID_TEXT, {"suggested_text": " "}, True, False, False),
    ],
    ids=[
        "question-low-confidence", "question-invalid", "question-placeholder",
        "matrix-invalid", "free-text", "free-text-empty", "code", "code-not-decoded",
        "grid-clean", "grid-ambiguous", "grid-unreadable", "grid-incomplete",
        "identifier-text", "identifier-text-empty",
    ],
)
def test_confirmable_and_bulk_acceptable_per_case(
    db, actor, submission_and_sheet, frozen_now, kind, payload, forces, confirmable, bulk
):
    submission, sheet = submission_and_sheet
    result = _make(db, sheet, kind, payload=payload, forces=forces)

    detail = get_submission_review(submission_id=submission.id, db=db, user=actor)
    obj = _objects(detail)[result.id]

    assert (obj.confirmable, obj.bulk_acceptable) == (confirmable, bulk)
    assert detail.bulk_acceptable_count == int(bulk)


def test_bulk_acceptable_count_leaves_resolved_results_out(db, actor, submission_and_sheet, frozen_now):
    submission, sheet = submission_and_sheet
    a = _make(db, sheet, QUESTION, y=1)
    _make(db, sheet, QUESTION, y=2)
    _make(db, sheet, FREE_TEXT, y=3)
    _act(db, submission, a, "CONFIRM", actor)

    detail = get_submission_review(submission_id=submission.id, db=db, user=actor)

    # The rule still holds for the resolved one; the count is of unresolved only.
    assert _objects(detail)[a.id].bulk_acceptable is True
    assert detail.bulk_acceptable_count == 1


# ---------------------------------------------------------------------------
# Statement counts
# ---------------------------------------------------------------------------


def _count(db, call) -> tuple[int, object]:
    statements: list[str] = []

    def _record(conn, cursor, statement, *args):
        statements.append(statement)

    engine = db.get_bind().engine
    db.expire_all()
    event.listen(engine, "before_cursor_execute", _record)
    try:
        out = call()
    finally:
        event.remove(engine, "before_cursor_execute", _record)
    return len(statements), out


def _populated(db, batch, template_page, actor, n: int) -> tuple[Submission, SubmissionObjectResult]:
    submission, sheet = _make_submission(db, batch, template_page)
    target = _make(db, sheet, QUESTION, y=0)
    for i in range(n):
        result = _make(db, sheet, (QUESTION, CODE, MATRIX_ROWS)[i % 3], y=10 + i)
        if i % 2:
            result.review_outcome = ReviewOutcome.CONFIRMED
            result.reviewed_by = actor.id
            result.reviewed_at = NOW
    db.flush()
    return submission, target


@pytest.mark.parametrize("action", ["CONFIRM", "SUBSTITUTE", "FLAG"])
def test_a_save_costs_the_same_however_many_results(db, actor, batch, template_page, frozen_now, action):
    small, small_target = _populated(db, batch, template_page, actor, 1)
    large, large_target = _populated(db, batch, template_page, actor, 12)

    def _save(submission, target):
        # Built before counting starts: only the save itself is measured.
        value = {"selected_option_ids": [str(_option_ids(db, target)[0])]} if action == "SUBSTITUTE" else None
        return lambda: _act(db, submission, target, action, actor, version=0, value=value)

    small_save, large_save = _save(small, small_target), _save(large, large_target)
    small_count, small_detail = _count(db, small_save)
    large_count, large_detail = _count(db, large_save)

    assert (small_detail.review_count, large_detail.review_count) == (2, 13)
    assert small_count == large_count


def test_accept_remaining_costs_the_same_however_many_results(db, actor, batch, template_page, frozen_now):
    small, _ = _populated(db, batch, template_page, actor, 1)
    large, _ = _populated(db, batch, template_page, actor, 12)

    small_count, small_out = _count(db, lambda: _accept(db, small, actor, version=0))
    large_count, large_out = _count(db, lambda: _accept(db, large, actor, version=0))

    assert (small_out.accepted_count, large_out.accepted_count) == (2, 7)
    assert small_count == large_count
