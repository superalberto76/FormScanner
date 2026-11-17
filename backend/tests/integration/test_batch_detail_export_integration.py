# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Batch results export, Detail export — DB-backed.

What is covered:

  * **rows** — the summary cells of each submission exactly as the Summary
    export gives them, then the columns per question;
  * **variants** — two variants of different length: the shorter leaves the
    columns beyond it empty; an UNRESOLVABLE submission has every column per
    question empty, and its variant adds no column;
  * **object types** — QUESTION, evaluative and plain MATRIX, FREE_TEXT_AREA
    and CODE read through the real loaders; a CODE holding a role has no
    column;
  * **coherence** — through a real close, the ``Dn_pt`` of the submission add
    up to its Points, also for a question linked across pages and read only
    on its copy of page 2;
  * **gate** — 409 ``NOT_EXPORT_READY``; 404 / 403;
  * **cost** — the same statements for 2 and for 6 submissions of a variant;
  * **endpoint** — file, media type, ``Content-Disposition``, the "Columns"
    sheet of the .xlsx, the snapshot called.
"""
from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from openpyxl import load_workbook
from sqlalchemy import event, func, select

from app.api.v1.endpoints.submission_batches import export_batch_detail
from app.api.v1.endpoints.submissions import close_submission
from app.models.code_object_config import CodeObjectConfig
from app.models.enums import (
    CodeType,
    FormProjectStatus,
    FormProjectType,
    FormVariantStatus,
    MatrixSelectionRule,
    PageObjectRole,
    PageObjectType,
    PageOrderSource,
    ProjectRole,
    QuestionType,
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
from app.models.matrix_cell_def import MatrixCellDef
from app.models.matrix_col_def import MatrixColDef
from app.models.matrix_object_config import MatrixObjectConfig
from app.models.matrix_row_def import MatrixRowDef
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
from app.services.export_writers import SUMMARY_HEADERS, DetailTable

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_user(db, prefix: str) -> User:
    user = User(email=f"{prefix}-{uuid.uuid4().hex}@example.com", password_hash="x", role=UserRole.USER)
    db.add(user)
    db.flush()
    return user


@pytest.fixture
def actor(db) -> User:
    return _make_user(db, "detail")


@pytest.fixture
def project(db, actor) -> FormProject:
    project = FormProject(
        owner_id=actor.id,
        type=FormProjectType.EXAM,
        name="Detail project",
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
        name="Final exam",
        created_by_user_id=actor.id,
        confidence_threshold=0.8,
    )
    db.add(batch)
    db.flush()
    return batch


def _variant(db, project, actor, name: str, *page_indices: int) -> list[FormVariantPage]:
    version = 1 + (
        db.scalar(
            select(func.max(FormVariant.version_number)).where(
                FormVariant.form_project_id == project.id
            )
        )
        or 0
    )
    variant = FormVariant(
        form_project_id=project.id,
        root_key=f"root-{uuid.uuid4().hex[:8]}",
        name=name,
        status=FormVariantStatus.PUBLISHED,
        version_number=version,
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


def _object(db, page, object_type: PageObjectType, y: float, label: str | None = None) -> PageObject:
    obj = PageObject(
        form_variant_page_id=page.id,
        object_type=object_type,
        x=10.0,
        y=y,
        width=100.0,
        height=40.0,
        label=label,
    )
    db.add(obj)
    db.flush()
    return obj


def _question(db, page, *scores: float, y: float = 10.0, text: str = "Q") -> tuple[PageObject, list[uuid.UUID]]:
    obj = _object(db, page, PageObjectType.QUESTION, y)
    config = QuestionObjectConfig(
        page_object_id=obj.id, question_type=QuestionType.SINGLE_CHOICE, question_text=text
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


def _matrix(db, page, *, evaluative: bool, y: float, cells: dict[tuple[int, int], float]) -> PageObject:
    """A 2x2 ONE_PER_ROW grid, rows "R1", "R2", columns "Yes", "No"."""
    obj = _object(db, page, PageObjectType.MATRIX, y, label="Grid")
    config = MatrixObjectConfig(
        page_object_id=obj.id,
        rows_count=2,
        cols_count=2,
        evaluative=evaluative,
        selection_rule=MatrixSelectionRule.ONE_PER_ROW,
    )
    db.add(config)
    db.flush()
    db.add_all(
        [MatrixRowDef(matrix_object_config_id=config.id, order_index=i, label=f"R{i + 1}") for i in range(2)]
        + [
            MatrixColDef(matrix_object_config_id=config.id, order_index=i, label=label)
            for i, label in enumerate(("Yes", "No"))
        ]
        + [
            MatrixCellDef(matrix_object_config_id=config.id, row_index=r, col_index=c, score=score)
            for (r, c), score in cells.items()
        ]
    )
    db.flush()
    return obj


def _free_text(db, page, y: float) -> PageObject:
    obj = _object(db, page, PageObjectType.FREE_TEXT_AREA, y, label="Comment")
    db.add(FreeTextAreaConfig(page_object_id=obj.id))
    db.flush()
    return obj


def _code(db, page, y: float, *, role: PageObjectRole | None = None) -> PageObject:
    obj = _object(db, page, PageObjectType.CODE, y, label="Bench")
    db.add(CodeObjectConfig(page_object_id=obj.id, code_type=CodeType.QR, value="bench-7"))
    if role is not None:
        db.add(
            FormVariantRoleBinding(
                form_variant_id=page.form_variant_id, page_object_id=obj.id, role=role
            )
        )
    db.flush()
    return obj


_T0 = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)


def _submission(
    db,
    batch,
    bound_to: list[FormVariantPage],
    *,
    status: SubmissionStatus = SubmissionStatus.REVIEWED,
    variant_id: uuid.UUID | None = None,
    total_score: float | None = None,
    uploaded_minutes: int = 0,
    unresolvable_from: SubmissionStatus | None = None,
) -> tuple[Submission, list[SubmissionPage]]:
    if variant_id is None and bound_to:
        variant_id = bound_to[0].form_variant_id
    submission = Submission(
        submission_batch_id=batch.id,
        matched_form_variant_id=variant_id,
        status=status,
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


def _result(db, sheet, obj, result_type, detected, **fields) -> SubmissionObjectResult:
    result = SubmissionObjectResult(
        submission_page_id=sheet.id,
        page_object_id=obj.id,
        result_type=result_type,
        detected_payload_json=detected,
        confidence_score=fields.pop("confidence_score", 0.95),
        validity_forces_review=fields.pop("validity_forces_review", False),
        review_required=fields.pop("review_required", False),
        **fields,
    )
    db.add(result)
    db.flush()
    return result


def _answer(db, sheet, obj, *option_ids: uuid.UUID, pending: bool = False):
    return _result(
        db,
        sheet,
        obj,
        SubmissionResultType.QUESTION,
        {
            "selected_options": [{"option_id": str(i), "confidence": 0.95} for i in option_ids],
            "validity": "VALID" if option_ids else "NO_ANSWER",
        },
        confidence_score=0.5 if pending else 0.95,
        review_required=pending,
    )


def _table(db, batch, user) -> DetailTable:
    db.expire_all()
    return batch_export_service.export_detail_table(db, batch.id, user).table


def _by_header(table: DetailTable, row_index: int) -> dict[str, object]:
    return dict(zip(table.headers, table.rows[row_index].cells, strict=True))


# ---------------------------------------------------------------------------
# Rows and variants
# ---------------------------------------------------------------------------


def test_the_summary_cells_are_the_summary_export(db, actor, project, batch):
    pages = _variant(db, project, actor, "A", 1, 2)
    question, ids = _question(db, pages[0], 3.0, 0.0)
    _sub, sheets = _submission(db, batch, pages[:1], total_score=3.0)
    _answer(db, sheets[0], question, ids[0])

    table = _table(db, batch, actor)
    db.expire_all()
    summary = batch_export_service.export_summary(db, batch.id, actor).rows

    assert [row.summary for row in table.rows] == summary


def test_a_shorter_variant_leaves_the_columns_beyond_it_empty(db, actor, project, batch):
    a = _variant(db, project, actor, "A", 1)
    first, a_ids = _question(db, a[0], 2.0, 0.0, y=10.0, text="First")
    second, a2_ids = _question(db, a[0], 0.0, 1.0, y=80.0, text="Second")
    b = _variant(db, project, actor, "B", 1)
    only, b_ids = _question(db, b[0], 0.0, 4.0, text="Only")
    _sub, a_sheets = _submission(db, batch, a, total_score=3.0, uploaded_minutes=1)
    _answer(db, a_sheets[0], first, a_ids[0])
    _answer(db, a_sheets[0], second, a2_ids[1])
    _sub, b_sheets = _submission(db, batch, b, total_score=4.0, uploaded_minutes=2)
    _answer(db, b_sheets[0], only, b_ids[1])

    table = _table(db, batch, actor)

    assert table.headers == ("D1", "D1_pt", "D2", "D2_pt")
    assert table.rows[0].cells == ("A", 2.0, "B", 1.0)
    assert table.rows[1].cells == ("B", 4.0, None, None)
    assert table.legend_variants == ("A", "B")
    assert table.legend == [
        ("D1", ("p. 1 · First", "p. 1 · Only")),
        ("D2", ("p. 1 · Second", None)),
    ]


def test_an_unresolvable_submission_has_every_column_empty(db, actor, project, batch):
    a = _variant(db, project, actor, "A", 1)
    question, ids = _question(db, a[0], 2.0, 0.0)
    _sub, sheets = _submission(db, batch, a, total_score=2.0, uploaded_minutes=1)
    _answer(db, sheets[0], question, ids[0])
    # A variant with more questions, held only by an unresolvable submission.
    longer = _variant(db, project, actor, "Longer", 1)
    for y in (10.0, 50.0, 90.0):
        _question(db, longer[0], 1.0, 0.0, y=y)
    _submission(
        db,
        batch,
        longer,
        status=SubmissionStatus.UNRESOLVABLE,
        unresolvable_from=SubmissionStatus.UNASSIGNED,
        uploaded_minutes=2,
    )
    _submission(
        db,
        batch,
        [],
        status=SubmissionStatus.UNRESOLVABLE,
        unresolvable_from=SubmissionStatus.MATCH_FAILED,
        uploaded_minutes=3,
    )

    table = _table(db, batch, actor)

    assert table.headers == ("D1", "D1_pt")
    assert table.legend_variants == ("A",)
    assert [row.cells for row in table.rows] == [("A", 2.0), (None, None), (None, None)]


# ---------------------------------------------------------------------------
# Object types through the real loaders
# ---------------------------------------------------------------------------


def test_every_object_type_has_its_family(db, actor, project, batch):
    pages = _variant(db, project, actor, "A", 1, 2)
    question, ids = _question(db, pages[0], 1.0, 0.0, y=10.0)
    scored = _matrix(db, pages[0], evaluative=True, y=50.0, cells={(0, 0): 1.0, (1, 1): 2.0})
    survey = _matrix(db, pages[0], evaluative=False, y=90.0, cells={})
    text = _free_text(db, pages[1], y=10.0)
    code = _code(db, pages[1], y=50.0)
    _code(db, pages[0], y=1.0, role=PageObjectRole.VARIANT_IDENTIFIER)
    _sub, sheets = _submission(db, batch, pages, total_score=1.0)
    _answer(db, sheets[0], question, ids[1])
    rows = {
        "rows": [
            {"row_index": 0, "selected_cells": [{"col_index": 0, "confidence": 0.9}], "validity": "VALID"},
            {"row_index": 1, "selected_cells": [], "validity": "NO_ANSWER"},
        ]
    }
    _result(db, sheets[0], scored, SubmissionResultType.MATRIX, rows)
    _result(db, sheets[0], survey, SubmissionResultType.MATRIX, rows)
    _result(
        db,
        sheets[1],
        text,
        SubmissionResultType.FREE_TEXT,
        {"suggested_text": "Too long"},
        review_required=True,
        validity_forces_review=True,
        review_outcome=ReviewOutcome.CONFIRMED,
    )
    _result(
        db,
        sheets[1],
        code,
        SubmissionResultType.CODE,
        {"decoded_value": "bench-7", "decode_successful": True},
    )

    table = _table(db, batch, actor)

    assert _by_header(table, 0) == {
        "D1": "B",
        "D1_pt": 0.0,
        "D2": "Yes",
        "D2_pt": 1.0,
        "D3": "*",
        "D3_pt": 0.0,
        "M1": "Yes",
        "M2": "*",
        "T1": "Too long",
        "C1": "bench-7",
    }
    assert [described[0] for _name, described in table.legend] == [
        "p. 1 · Q",
        "p. 1 · Grid · R1",
        "p. 1 · Grid · R2",
        "p. 1 · Grid · R1",
        "p. 1 · Grid · R2",
        "p. 2 · Comment",
        "p. 2 · Bench",
    ]


def test_a_missing_page_is_the_symbol_and_zero(db, actor, project, batch):
    pages = _variant(db, project, actor, "A", 1, 2)
    question, ids = _question(db, pages[0], 1.0, 0.0)
    _question(db, pages[1], 5.0, 0.0)
    _sub, sheets = _submission(db, batch, pages[:1], total_score=1.0)
    _answer(db, sheets[0], question, ids[0])

    table = _table(db, batch, actor)

    assert table.rows[0].summary.missing_pages == (2,)
    assert table.rows[0].cells == ("A", 1.0, "*", 0.0)


# ---------------------------------------------------------------------------
# Coherence with the close
# ---------------------------------------------------------------------------


def test_the_points_per_question_add_up_to_the_points(db, actor, project, batch):
    pages = _variant(db, project, actor, "A", 1)
    right, right_ids = _question(db, pages[0], 2.0, -1.0, y=10.0)
    wrong, wrong_ids = _question(db, pages[0], 3.0, -1.0, y=50.0)
    pending, pending_ids = _question(db, pages[0], 4.0, 0.0, y=90.0)
    matrix = _matrix(db, pages[0], evaluative=True, y=130.0, cells={(0, 0): 1.5, (1, 1): 0.5})
    submission, sheets = _submission(db, batch, pages, status=SubmissionStatus.SCANNED)
    _answer(db, sheets[0], right, right_ids[0])
    _answer(db, sheets[0], wrong, wrong_ids[1])
    _answer(db, sheets[0], pending, pending_ids[0], pending=True)
    _result(
        db,
        sheets[0],
        matrix,
        SubmissionResultType.MATRIX,
        {
            "rows": [
                {"row_index": 0, "selected_cells": [{"col_index": 0, "confidence": 0.9}], "validity": "VALID"},
                {"row_index": 1, "selected_cells": [{"col_index": 1, "confidence": 0.9}], "validity": "VALID"},
            ]
        },
    )
    db.refresh(submission)
    close_submission(
        submission_id=submission.id,
        data=SubmissionCloseRequest(review_version=submission.review_version),
        db=db,
        user=actor,
    )

    table = _table(db, batch, actor)

    row = _by_header(table, 0)
    points = [value for header, value in row.items() if header.endswith("_pt")]
    assert table.rows[0].summary.points == 2.0 - 1.0 + 0.0 + 1.5 + 0.5
    assert sum(points) == table.rows[0].summary.points
    assert row["D3"] == "*"


def test_a_linked_question_read_on_page_two_adds_up_to_the_points(db, actor, project, batch):
    """One question on both pages, page 1 missing. The reading is on the
    copy of page 2, whose options have ids of their own."""
    pages = _variant(db, project, actor, "A", 1, 2)
    group = uuid.uuid4()
    first, _first_ids = _question(db, pages[0], 0.0, 3.0, text="Shared")
    again, again_ids = _question(db, pages[1], 0.0, 3.0, text="Shared")
    for copy in (first, again):
        copy.shared_group_id = group
        copy.shared_across_pages = True
    db.flush()
    submission, sheets = _submission(db, batch, pages[1:], status=SubmissionStatus.SCANNED)
    _answer(db, sheets[0], again, again_ids[1])
    db.refresh(submission)
    close_submission(
        submission_id=submission.id,
        data=SubmissionCloseRequest(review_version=submission.review_version),
        db=db,
        user=actor,
    )

    table = _table(db, batch, actor)

    assert table.headers == ("D1", "D1_pt")
    assert table.rows[0].cells == ("B", 3.0)
    assert table.rows[0].summary.points == 3.0
    assert table.rows[0].summary.missing_pages == (1,)


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def _refusal(db, batch_id, user) -> HTTPException:
    db.expire_all()
    with pytest.raises(HTTPException) as caught:
        batch_export_service.export_detail_table(db, batch_id, user)
    return caught.value


def test_a_batch_in_progress_is_refused(db, actor, project, batch):
    pages = _variant(db, project, actor, "A", 1)
    _submission(db, batch, pages, total_score=0.0)
    _submission(db, batch, pages, status=SubmissionStatus.SCANNED)

    refusal = _refusal(db, batch.id, actor)

    assert (refusal.status_code, refusal.detail["code"]) == (409, "NOT_EXPORT_READY")


def test_a_non_member_and_an_unknown_batch_are_refused(db, actor, project, batch):
    pages = _variant(db, project, actor, "A", 1)
    _submission(db, batch, pages, total_score=0.0)

    assert _refusal(db, batch.id, _make_user(db, "stranger")).status_code == 403
    assert _refusal(db, uuid.uuid4(), actor).status_code == 404


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


def test_the_cost_does_not_grow_with_the_submissions(db, actor, project):
    pages = _variant(db, project, actor, "A", 1)
    question, ids = _question(db, pages[0], 1.0, 0.0)
    text = _free_text(db, pages[0], y=60.0)
    matrix = _matrix(db, pages[0], evaluative=True, y=120.0, cells={(0, 0): 1.0})

    def _filled_batch(count: int) -> SubmissionBatch:
        batch = SubmissionBatch(form_project_id=project.id, name=f"b{count}", created_by_user_id=actor.id)
        db.add(batch)
        db.flush()
        for minute in range(count):
            _sub, sheets = _submission(db, batch, pages, total_score=1.0, uploaded_minutes=minute)
            _answer(db, sheets[0], question, ids[0], pending=minute % 2 == 0)
            _result(db, sheets[0], text, SubmissionResultType.FREE_TEXT, {"suggested_text": "x"}, review_required=True)
            _result(db, sheets[0], matrix, SubmissionResultType.MATRIX, {"rows": []})
        return batch

    small, large = _filled_batch(2), _filled_batch(6)

    small_count = _count_statements(
        db, lambda: batch_export_service.export_detail_table(db, small.id, actor)
    )
    large_count = _count_statements(
        db, lambda: batch_export_service.export_detail_table(db, large.id, actor)
    )

    assert small_count == large_count


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
def no_snapshot(monkeypatch):
    calls: list = []
    monkeypatch.setattr(batch_export_service, "begin_read_snapshot", calls.append)
    return calls


def _filled(db, project, actor, batch):
    pages = _variant(db, project, actor, "A", 1)
    question, ids = _question(db, pages[0], 4.0, 0.0, text="Capital?")
    _sub, sheets = _submission(db, batch, pages, total_score=4.0)
    _answer(db, sheets[0], question, ids[0])


def _call(db, batch, user, export_format, dialect=CsvDialect.COMMA):
    db.expire_all()
    return export_batch_detail(
        batch_id=batch.id, format=export_format, csv_dialect=dialect, db=db, user=user
    )


def test_the_csv_endpoint_returns_the_file(db, actor, project, batch, no_snapshot):
    _filled(db, project, actor, batch)

    response = _call(db, batch, actor, ExportFormat.CSV)

    assert no_snapshot == [db]
    assert response.media_type == "text/csv; charset=utf-8"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="Detail_Final_exam_')
    assert disposition.split(";")[1].strip().endswith('Z.csv"')
    header, line = list(csv.reader(io.StringIO(response.body.decode("utf-8-sig"))))
    assert tuple(header) == SUMMARY_HEADERS + ("D1", "D1_pt")
    assert line[len(SUMMARY_HEADERS):] == ["A", "4"]


def test_the_xlsx_endpoint_has_the_detail_and_the_columns_sheets(
    db, actor, project, batch, no_snapshot
):
    _filled(db, project, actor, batch)

    response = _call(db, batch, actor, ExportFormat.XLSX)

    assert response.headers["content-disposition"].split(";")[1].strip().endswith('Z.xlsx"')
    book = load_workbook(io.BytesIO(response.body))
    assert book.sheetnames == ["Detail", "Columns"]
    assert [cell.value for cell in book["Detail"][2]][len(SUMMARY_HEADERS):] == ["A", 4]
    assert [[c.value for c in row] for row in book["Columns"].iter_rows()] == [
        ["Column", "A"],
        ["D1", "p. 1 · Capital?"],
    ]


def test_the_endpoint_refuses_a_batch_not_ready(db, actor, project, batch, no_snapshot):
    pages = _variant(db, project, actor, "A", 1)
    _submission(db, batch, pages, status=SubmissionStatus.SCANNED)

    with pytest.raises(HTTPException) as caught:
        _call(db, batch, actor, ExportFormat.CSV)

    assert caught.value.detail["code"] == "NOT_EXPORT_READY"
