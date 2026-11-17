# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Batch results export, Summary export — DB-backed.

What is covered:

  * **rows** — one per submission, in upload order: short id, candidate,
    variant name, points from ``total_score``, the variant's maximum,
    percentage, status code, missing pages, unreviewed objects (the results
    left unresolved at the close), batch name, upload time;
  * **empty cells** — UNRESOLVABLE (from MATCH_FAILED without a variant, from
    UNASSIGNED with one): no score, no unreviewed count; maximum 0 → no
    percentage; a closed submission with ``total_score`` NULL keeps its
    maximum only; a negative score stays negative;
  * **coherence** — through a real close, the export shows the score the
    review page was showing;
  * **gate** — 409 ``NOT_EXPORT_READY`` on a batch in progress, empty, or of
    UNRESOLVABLE submissions only; 404 / 403;
  * **cost** — the same statements for 2 and for 6 submissions of a variant;
  * **endpoint** — file, media type and ``Content-Disposition`` for both
    formats and both CSV dialects;
  * **snapshot** — :func:`begin_read_snapshot` leaves the
    session in a REPEATABLE READ, READ ONLY transaction. Tested on a session
    of its own: inside the fixture's outer transaction ``SET TRANSACTION`` is
    not allowed, which is also why the endpoint tests replace it.
"""
from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from openpyxl import load_workbook
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session

from app.api.v1.endpoints.submission_batches import export_batch_summary
from app.api.v1.endpoints.submissions import close_submission, get_submission_review
from app.models.enums import (
    FormProjectStatus,
    FormProjectType,
    FormVariantStatus,
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
from app.models.page_object import PageObject
from app.models.question_object_config import QuestionObjectConfig
from app.models.question_option import QuestionOption
from app.models.submission import Submission
from app.models.submission_batch import SubmissionBatch
from app.models.submission_object_result import SubmissionObjectResult
from app.models.submission_page import SubmissionPage
from app.models.user import User
from app.schemas.submission import CsvDialect, ExportFormat, SubmissionCloseRequest
from app.services import batch_export_service
from app.services.export_writers import SUMMARY_HEADERS, SummaryRow

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_user(db, prefix: str) -> User:
    user = User(
        email=f"{prefix}-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.USER,
    )
    db.add(user)
    db.flush()
    return user


@pytest.fixture
def actor(db) -> User:
    return _make_user(db, "export")


@pytest.fixture
def project(db, actor) -> FormProject:
    project = FormProject(
        owner_id=actor.id,
        type=FormProjectType.EXAM,
        name="Export project",
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
        name="Mid-term exam",
        created_by_user_id=actor.id,
        confidence_threshold=0.8,
    )
    db.add(batch)
    db.flush()
    return batch


def _next_version(db, project) -> int:
    return 1 + (
        db.scalar(
            select(func.max(FormVariant.version_number)).where(
                FormVariant.form_project_id == project.id
            )
        )
        or 0
    )


def _variant(db, project, actor, name: str, *page_indices: int) -> list[FormVariantPage]:
    """A PUBLISHED variant called *name* and its template pages."""
    variant = FormVariant(
        form_project_id=project.id,
        root_key=f"root-{uuid.uuid4().hex[:8]}",
        name=name,
        status=FormVariantStatus.PUBLISHED,
        version_number=_next_version(db, project),
        created_by_user_id=actor.id,
    )
    db.add(variant)
    db.flush()
    pages = [
        FormVariantPage(form_variant_id=variant.id, page_index=index, width_px=794, height_px=1123)
        for index in page_indices
    ]
    db.add_all(pages)
    db.flush()
    return pages


@pytest.fixture
def pages(db, project, actor) -> list[FormVariantPage]:
    """Variant "A": two template pages, 1 and 2."""
    return _variant(db, project, actor, "A", 1, 2)


def _question(db, page: FormVariantPage, *scores: float) -> tuple[PageObject, list[uuid.UUID]]:
    """A single-choice QUESTION on *page* with these option scores."""
    obj = PageObject(
        form_variant_page_id=page.id,
        object_type=PageObjectType.QUESTION,
        x=10.0,
        y=10.0,
        width=100.0,
        height=40.0,
    )
    db.add(obj)
    db.flush()
    config = QuestionObjectConfig(
        page_object_id=obj.id, question_type=QuestionType.SINGLE_CHOICE, question_text="Q"
    )
    db.add(config)
    db.flush()
    options = [
        QuestionOption(
            question_object_config_id=config.id,
            order_index=index,
            option_text=f"option {index}",
            is_correct=score > 0,
            score=score,
        )
        for index, score in enumerate(scores)
    ]
    db.add_all(options)
    db.flush()
    return obj, [option.id for option in options]


_T0 = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)


def _submission(
    db,
    batch,
    bound_to: list[FormVariantPage],
    *,
    status: SubmissionStatus = SubmissionStatus.REVIEWED,
    variant_id: uuid.UUID | None = None,
    candidate: str | None = None,
    total_score: float | None = None,
    uploaded_minutes: int = 0,
    unresolvable_from: SubmissionStatus | None = None,
) -> tuple[Submission, list[SubmissionPage]]:
    """A submission with one sheet per template page of *bound_to*, uploaded
    *uploaded_minutes* after ``_T0``."""
    if variant_id is None and bound_to:
        variant_id = bound_to[0].form_variant_id
    submission = Submission(
        submission_batch_id=batch.id,
        matched_form_variant_id=variant_id,
        status=status,
        respondent_identifier=candidate,
        total_score=total_score,
        unresolvable_from_status=unresolvable_from,
        created_at=_T0 + timedelta(minutes=uploaded_minutes),
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
    db, sheet: SubmissionPage, obj: PageObject, *option_ids: uuid.UUID, pending: bool = False
) -> SubmissionObjectResult:
    """A QUESTION reading; *pending*: in review and never resolved."""
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
        confidence_score=0.5 if pending else 0.95,
        validity_forces_review=False,
        review_required=pending,
    )
    db.add(result)
    db.flush()
    return result


def _rows(db, batch, user) -> list[SummaryRow]:
    db.expire_all()
    return batch_export_service.export_summary(db, batch.id, user).rows


def _refusal(db, batch, user) -> HTTPException:
    db.expire_all()
    with pytest.raises(HTTPException) as caught:
        batch_export_service.export_summary(db, batch.id, user)
    return caught.value


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


def test_a_closed_submission_has_its_whole_row(db, actor, batch, pages):
    first, first_ids = _question(db, pages[0], 3.0, 0.0)
    second, second_ids = _question(db, pages[0], 1.0, 0.0)
    submission, sheets = _submission(
        db, batch, pages, candidate="S12345", total_score=3.0, uploaded_minutes=5
    )
    _answer(db, sheets[0], first, first_ids[0])
    _answer(db, sheets[0], second, second_ids[0], pending=True)

    [row] = _rows(db, batch, actor)

    full = str(submission.id)
    assert row == SummaryRow(
        submission=f"{full[:4]}…{full[-4:]}",
        candidate="S12345",
        variant="A",
        points=3.0,
        max_points=4.0,
        percentage=75.0,
        status="REVIEWED",
        missing_pages=(),
        unreviewed_objects=1,
        batch="Mid-term exam",
        scanned_at=_T0 + timedelta(minutes=5),
    )


def test_a_fully_verified_submission_has_zero_unreviewed_objects(db, actor, batch, pages):
    question, ids = _question(db, pages[0], 2.0, 0.0)
    _submission_, sheets = _submission(db, batch, pages, total_score=2.0)
    _answer(db, sheets[0], question, ids[0])

    [row] = _rows(db, batch, actor)

    assert row.unreviewed_objects == 0
    assert row.candidate is None


def test_missing_pages_are_the_template_page_numbers(db, actor, project, batch):
    three = _variant(db, project, actor, "Three pages", 1, 2, 3)
    _question(db, three[0], 1.0, 0.0)
    _submission(db, batch, three[:1], total_score=1.0)

    [row] = _rows(db, batch, actor)

    assert row.missing_pages == (2, 3)


def test_rows_follow_the_upload_order(db, actor, batch, pages):
    _question(db, pages[0], 1.0, 0.0)
    late, _ = _submission(db, batch, pages, candidate="late", total_score=1.0, uploaded_minutes=9)
    early, _ = _submission(db, batch, pages, candidate="early", total_score=0.0, uploaded_minutes=1)

    assert [row.candidate for row in _rows(db, batch, actor)] == ["early", "late"]


def test_each_submission_has_the_maximum_of_its_own_variant(db, actor, project, batch, pages):
    _question(db, pages[0], 2.0, 0.0)
    other = _variant(db, project, actor, "B", 1)
    _question(db, other[0], 5.0, 0.0)
    _submission(db, batch, pages, candidate="a", total_score=1.0, uploaded_minutes=1)
    _submission(db, batch, other, candidate="b", total_score=5.0, uploaded_minutes=2)

    rows = _rows(db, batch, actor)

    assert [(r.variant, r.max_points, r.percentage) for r in rows] == [
        ("A", 2.0, 50.0),
        ("B", 5.0, 100.0),
    ]


# ---------------------------------------------------------------------------
# Empty cells
# ---------------------------------------------------------------------------


def test_an_unresolvable_submission_without_a_variant_has_no_score(db, actor, batch, pages):
    _question(db, pages[0], 1.0, 0.0)
    _submission(db, batch, pages, total_score=1.0, uploaded_minutes=1)
    _submission(
        db,
        batch,
        [],
        status=SubmissionStatus.UNRESOLVABLE,
        unresolvable_from=SubmissionStatus.MATCH_FAILED,
        uploaded_minutes=2,
    )

    row = _rows(db, batch, actor)[1]

    assert row.status == "UNRESOLVABLE"
    assert (row.variant, row.points, row.max_points, row.percentage) == (None, None, None, None)
    assert row.unreviewed_objects is None
    assert row.missing_pages == ()


def test_an_unresolvable_submission_keeps_its_variant_and_missing_pages(
    db, actor, batch, pages
):
    _question(db, pages[0], 1.0, 0.0)
    _submission(db, batch, pages, total_score=1.0, uploaded_minutes=1)
    # An ungroupable sheet with a variant read on it, then marked unresolvable.
    _submission(
        db,
        batch,
        pages[:1],
        status=SubmissionStatus.UNRESOLVABLE,
        unresolvable_from=SubmissionStatus.UNASSIGNED,
        candidate="S9",
        uploaded_minutes=2,
    )

    row = _rows(db, batch, actor)[1]

    assert (row.candidate, row.variant, row.missing_pages) == ("S9", "A", (2,))
    assert (row.points, row.max_points, row.percentage, row.unreviewed_objects) == (
        None,
        None,
        None,
        None,
    )


def test_a_maximum_of_zero_has_no_percentage(db, actor, batch, pages):
    _submission(db, batch, pages, total_score=0.0)

    [row] = _rows(db, batch, actor)

    assert (row.points, row.max_points, row.percentage) == (0.0, 0.0, None)


def test_a_closed_submission_without_a_saved_score_keeps_its_maximum(db, actor, batch, pages):
    _question(db, pages[0], 4.0, 0.0)
    _submission(db, batch, pages, total_score=None)

    [row] = _rows(db, batch, actor)

    assert (row.points, row.max_points, row.percentage) == (None, 4.0, None)


def test_a_negative_score_stays_negative(db, actor, batch, pages):
    _question(db, pages[0], 4.0, 0.0)
    _submission(db, batch, pages, total_score=-1.0)

    [row] = _rows(db, batch, actor)

    assert (row.points, row.percentage) == (-1.0, -25.0)


# ---------------------------------------------------------------------------
# Coherence with the review page
# ---------------------------------------------------------------------------


def test_the_export_shows_the_score_the_review_page_showed_at_the_close(
    db, actor, batch, pages
):
    first, first_ids = _question(db, pages[0], 2.0, 0.0)
    second, second_ids = _question(db, pages[1], 3.0, 0.0)
    submission, sheets = _submission(db, batch, pages, status=SubmissionStatus.SCANNED)
    _answer(db, sheets[0], first, first_ids[0])
    _answer(db, sheets[1], second, second_ids[0], pending=True)
    db.expire_all()
    shown = get_submission_review(submission_id=submission.id, db=db, user=actor).score
    db.refresh(submission)
    close_submission(
        submission_id=submission.id,
        data=SubmissionCloseRequest(review_version=submission.review_version),
        db=db,
        user=actor,
    )

    [row] = _rows(db, batch, actor)

    assert (row.points, row.max_points, row.percentage) == (
        shown.raw_score,
        shown.max_possible_score,
        shown.percentage,
    )
    assert row.unreviewed_objects == 1


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def test_a_batch_in_progress_is_refused_with_its_counts(db, actor, batch, pages):
    _submission(db, batch, pages, total_score=0.0)
    _submission(db, batch, pages, status=SubmissionStatus.SCANNED)

    refusal = _refusal(db, batch, actor)

    assert refusal.status_code == 409
    assert refusal.detail["code"] == "NOT_EXPORT_READY"
    assert refusal.detail["in_review"] == 1


@pytest.mark.parametrize("unresolvable", [0, 2])
def test_nothing_to_export_is_refused(db, actor, batch, unresolvable):
    for _ in range(unresolvable):
        _submission(
            db,
            batch,
            [],
            status=SubmissionStatus.UNRESOLVABLE,
            unresolvable_from=SubmissionStatus.MATCH_FAILED,
        )

    assert _refusal(db, batch, actor).detail["code"] == "NOT_EXPORT_READY"


def test_a_non_member_is_refused(db, batch, pages):
    _submission(db, batch, pages, total_score=0.0)
    stranger = _make_user(db, "stranger")

    # 403, as every other read of the batch (require_project_member).
    assert _refusal(db, batch, stranger).status_code == 403


def test_an_unknown_batch_is_404(db, actor):
    db.expire_all()
    with pytest.raises(HTTPException) as caught:
        batch_export_service.export_summary(db, uuid.uuid4(), actor)
    assert caught.value.status_code == 404


def test_a_closed_project_can_still_export(db, actor, project, batch, pages):
    _submission(db, batch, pages, total_score=0.0)
    project.status = FormProjectStatus.CLOSED
    db.flush()

    assert len(_rows(db, batch, actor)) == 1


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


def _count_statements(db, call) -> int:
    statements: list[str] = []

    def _record(conn, cursor, statement, *args):
        statements.append(statement)

    engine = db.get_bind().engine
    event.listen(engine, "before_cursor_execute", _record)
    try:
        db.expire_all()
        call()
    finally:
        event.remove(engine, "before_cursor_execute", _record)
    return len(statements)


def test_the_cost_does_not_grow_with_the_submissions(db, actor, project, pages):
    question, ids = _question(db, pages[0], 1.0, 0.0)

    def _filled_batch(count: int) -> SubmissionBatch:
        batch = SubmissionBatch(
            form_project_id=project.id, name=f"b{count}", created_by_user_id=actor.id
        )
        db.add(batch)
        db.flush()
        for minute in range(count):
            _sub, sheets = _submission(
                db, batch, pages, total_score=1.0, uploaded_minutes=minute
            )
            _answer(db, sheets[0], question, ids[0], pending=minute % 2 == 0)
        return batch

    small, large = _filled_batch(2), _filled_batch(6)

    small_count = _count_statements(
        db, lambda: batch_export_service.export_summary(db, small.id, actor)
    )
    large_count = _count_statements(
        db, lambda: batch_export_service.export_summary(db, large.id, actor)
    )

    assert small_count == large_count


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
def no_snapshot(monkeypatch):
    """The fixture's outer transaction forbids ``SET TRANSACTION``; the
    snapshot itself is tested below on a session of its own."""
    calls: list[Session] = []
    monkeypatch.setattr(batch_export_service, "begin_read_snapshot", calls.append)
    return calls


def _call(db, batch, user, export_format, dialect=CsvDialect.COMMA):
    db.expire_all()
    return export_batch_summary(
        batch_id=batch.id, format=export_format, csv_dialect=dialect, db=db, user=user
    )


def test_the_csv_endpoint_returns_the_file(db, actor, batch, pages, no_snapshot):
    _question(db, pages[0], 4.0, 0.0)
    _submission(db, batch, pages, candidate="S1", total_score=3.0)

    response = _call(db, batch, actor, ExportFormat.CSV)

    assert no_snapshot == [db]
    assert response.media_type == "text/csv; charset=utf-8"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="Summary_Mid-term_exam_')
    assert disposition.split(";")[1].strip().endswith('Z.csv"')
    body = response.body.decode("utf-8-sig")
    header, line = list(csv.reader(io.StringIO(body)))
    assert tuple(header) == SUMMARY_HEADERS
    assert line[1:7] == ["S1", "A", "3", "4", "75.0", "REVIEWED"]


def test_the_semicolon_dialect_writes_decimal_commas(db, actor, batch, pages, no_snapshot):
    _question(db, pages[0], 3.0, 0.0)
    _submission(db, batch, pages, total_score=1.5)

    response = _call(db, batch, actor, ExportFormat.CSV, CsvDialect.SEMICOLON)

    _header, line = list(
        csv.reader(io.StringIO(response.body.decode("utf-8-sig")), delimiter=";")
    )
    assert line[3:6] == ["1,5", "3", "50,0"]


def test_the_xlsx_endpoint_returns_a_workbook(db, actor, batch, pages, no_snapshot):
    _question(db, pages[0], 4.0, 0.0)
    _submission(db, batch, pages, candidate="S1", total_score=3.0)

    response = _call(db, batch, actor, ExportFormat.XLSX)

    assert response.media_type == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert response.headers["content-disposition"].split(";")[1].strip().endswith('Z.xlsx"')
    sheet = load_workbook(io.BytesIO(response.body)).active
    values = [cell.value for cell in sheet[2]]
    assert values[1:7] == ["S1", "A", 3, 4, 75, "REVIEWED"]


def test_the_endpoint_refuses_a_batch_not_ready(db, actor, batch, pages, no_snapshot):
    _submission(db, batch, pages, status=SubmissionStatus.SCANNED)

    with pytest.raises(HTTPException) as caught:
        _call(db, batch, actor, ExportFormat.CSV)

    assert caught.value.detail["code"] == "NOT_EXPORT_READY"


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


def test_the_snapshot_is_a_repeatable_read_read_only_transaction(engine):
    session = Session(bind=engine)
    try:
        # What the request has done before: the authentication read.
        session.execute(text("SELECT 1"))
        batch_export_service.begin_read_snapshot(session)

        assert session.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        assert session.scalar(text("SHOW transaction_read_only")) == "on"
    finally:
        session.rollback()
        session.close()
