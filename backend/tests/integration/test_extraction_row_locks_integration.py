# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests: an extraction run does not hold the rows of
the submissions it has walked.

Review reads a submission ``FOR UPDATE`` (opening with the soft lock, every
save through ``guard_review_save``). A run is one transaction of minutes, so
any row of a SCANNED submission it writes or locks before the end is a review
waiting until the run commits. What this suite pins down, on the SQL the run
emits while it walks:

  * **the attempt time** — ``extraction_attempted_at`` of a submission already
    walked is not written when the run flushes for the NEXT submission's
    candidate (cause 1); it is written once the walk ends, the same instant on
    every submission walked, never on one skipped;
  * **the candidate** — a submission that may receive a candidate but whose
    respondent ID gives none (no respondent ID at all, as in an anonymous
    questionnaire, or one with a "?") is not read ``FOR UPDATE`` (cause 2).

The session runs with ``autoflush`` off, as in production
(``app/db/session.py``): with it on, every query of the walk would flush and
mask what the run itself writes. The SQL is captured on the test connection
(``before_cursor_execute``) rather than observed from a second connection:
the test data is never committed (``conftest.py``), so another connection
could not see the rows, let alone wait on them. The two-connection check was
run once, outside the suite.

Real synthetic scans for the candidate path, the same geometry as
``test_submission_candidate_from_reading_integration``. Skipped without
TEST_DATABASE_URL, like the rest of tests/integration.
"""
from __future__ import annotations

import io
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image, ImageDraw
from sqlalchemy import event

from app.api.v1.endpoints.submission_batches import run_batch_extraction
from app.core.config import settings
from app.models.enums import (
    FormProjectStatus,
    FormProjectType,
    FormVariantStatus,
    MatrixSelectionRule,
    PageObjectRole,
    PageObjectType,
    ProjectRole,
    SubmissionStatus,
    UserRole,
)
from app.models.form_project import FormProject
from app.models.form_project_member import FormProjectMember
from app.models.form_variant import FormVariant
from app.models.form_variant_page import FormVariantPage
from app.models.form_variant_role_binding import FormVariantRoleBinding
from app.models.matrix_col_def import MatrixColDef
from app.models.matrix_object_config import MatrixObjectConfig
from app.models.matrix_row_def import MatrixRowDef
from app.models.page_object import PageObject
from app.models.submission import Submission
from app.models.submission_batch import SubmissionBatch
from app.models.submission_page import SubmissionPage
from app.models.user import User
from app.services import extraction_orchestrator_service
from app.utils import alignment_marks, question_layout

TEMPLATE_W = 794
TEMPLATE_H = 1123

# A respondent-ID grid: 10 digit rows ("0".."9"), 5 columns.
OBJ_X = 60.0
OBJ_Y = 100.0
OBJ_W = 400.0
OBJ_H = 900.0
ROWS = 10
COLS = 5

WHITE = 255
BLACK = 0

# Column c inked on row CLEAN_ROWS[c]: the respondent ID "37095".
CLEAN_ROWS = (3, 7, 0, 9, 5)
CLEAN_VALUE = "37095"

# Submissions are walked in ingestion order (created_at, then id); rows created
# in one transaction share now(), so the order is set explicitly.
EARLIER = datetime(2026, 10, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


@pytest.fixture
def pages_dir(tmp_path, monkeypatch):
    target = tmp_path / "submission_pages"
    target.mkdir()
    monkeypatch.setattr(settings, "submission_pages_dir", str(target))
    return target


@pytest.fixture
def production_session(db):
    """The test session with autoflush off, like ``SessionLocal``."""
    db.autoflush = False
    return db


@pytest.fixture
def actor(db) -> User:
    u = User(
        email=f"rowlocks-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.USER,
    )
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def project(db, actor) -> FormProject:
    p = FormProject(
        owner_id=actor.id,
        type=FormProjectType.EXAM,
        name="Row locks",
        status=FormProjectStatus.ACTIVE,
    )
    db.add(p)
    db.flush()
    db.add(
        FormProjectMember(project_id=p.id, user_id=actor.id, role=ProjectRole.OWNER)
    )
    db.flush()
    return p


@pytest.fixture
def variant(db, project, actor) -> FormVariant:
    v = FormVariant(
        form_project_id=project.id,
        root_key="root",
        name="v1",
        status=FormVariantStatus.PUBLISHED,
        version_number=1,
        created_by_user_id=actor.id,
    )
    db.add(v)
    db.flush()
    return v


def _make_template_page(db, variant, page_index: int) -> FormVariantPage:
    page = FormVariantPage(
        form_variant_id=variant.id,
        page_index=page_index,
        width_px=TEMPLATE_W,
        height_px=TEMPLATE_H,
    )
    db.add(page)
    db.flush()
    return page


@pytest.fixture
def respondent_id_page(db, variant) -> FormVariantPage:
    """A template page holding the respondent-ID grid: a MATRIX bound to
    RESPONDENT_IDENTIFIER, rows labelled "0".."9"."""
    page = _make_template_page(db, variant, 1)
    obj = PageObject(
        form_variant_page_id=page.id,
        object_type=PageObjectType.MATRIX,
        x=OBJ_X,
        y=OBJ_Y,
        width=OBJ_W,
        height=OBJ_H,
    )
    db.add(obj)
    db.flush()
    config = MatrixObjectConfig(
        page_object_id=obj.id,
        rows_count=ROWS,
        cols_count=COLS,
        selection_rule=MatrixSelectionRule.FREE,
    )
    db.add(config)
    db.flush()
    for index in range(ROWS):
        db.add(
            MatrixRowDef(
                matrix_object_config_id=config.id, order_index=index, label=str(index)
            )
        )
    for index in range(COLS):
        db.add(
            MatrixColDef(
                matrix_object_config_id=config.id, order_index=index, label=f"C{index}"
            )
        )
    db.add(
        FormVariantRoleBinding(
            form_variant_id=variant.id,
            page_object_id=obj.id,
            role=PageObjectRole.RESPONDENT_IDENTIFIER,
        )
    )
    db.flush()
    db.refresh(page)
    return page


@pytest.fixture
def batch(db, project, actor) -> SubmissionBatch:
    b = SubmissionBatch(
        form_project_id=project.id,
        name="Batch 1",
        created_by_user_id=actor.id,
        confidence_threshold=0.3,
    )
    db.add(b)
    db.flush()
    return b


def _cell_center(row: int, col: int) -> tuple[float, float]:
    """Where the export prints the (row, col) bubble, in template space — the
    raw formula, as in ``test_respondent_identifier_extraction_integration``."""
    label_w = min(OBJ_W * 0.25, 120.0)
    grid_x = OBJ_X + label_w
    grid_y = OBJ_Y + 16 + 16  # LABEL_H + HEADER_H
    cell_w = max(OBJ_W - label_w, 1.0) / COLS
    cell_h = max((OBJ_Y + OBJ_H) - grid_y, 1.0) / ROWS
    return (
        grid_x + col * cell_w + cell_w / 2,
        grid_y + row * cell_h + cell_h / 2,
    )


def _draw_alignment_marks(draw: ImageDraw.ImageDraw, scale: float) -> None:
    """The four marks every exported page prints. Without them the page is
    NOT_ALIGNED and its respondent ID goes to review whatever its marks, which
    is not what these tests are about."""
    for corner, (x, y, w, h) in alignment_marks.mark_rects(TEMPLATE_W, TEMPLATE_H).items():
        draw.rectangle(
            [x * scale, y * scale, (x + w) * scale - 1, (y + h) * scale - 1], fill=BLACK
        )
        if corner == alignment_marks.DISTINCT_CORNER:
            hx, hy, hw, hh = alignment_marks.hole_rect((x, y, w, h))
            draw.rectangle(
                [hx * scale, hy * scale, (hx + hw) * scale - 1, (hy + hh) * scale - 1],
                fill=WHITE,
            )


def _draw_scan(filled: dict[tuple[int, int], float], *, scale: float = 4.0) -> bytes:
    """A synthetic scan: every bubble outlined, *filled* inked to a fraction of
    the printed radius."""
    img = Image.new("L", (int(TEMPLATE_W * scale), int(TEMPLATE_H * scale)), WHITE)
    draw = ImageDraw.Draw(img)
    printed_radius = question_layout.CIRCLE_R * scale
    for row in range(ROWS):
        for col in range(COLS):
            cx, cy = _cell_center(row, col)
            cx, cy = cx * scale, cy * scale
            draw.ellipse(
                [cx - printed_radius, cy - printed_radius,
                 cx + printed_radius, cy + printed_radius],
                outline=BLACK,
                width=max(int(scale / 2), 1),
            )
    for (row, col), fraction in filled.items():
        cx, cy = _cell_center(row, col)
        cx, cy = cx * scale, cy * scale
        r = printed_radius * fraction
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=BLACK)
    _draw_alignment_marks(draw, scale)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _clean_marks() -> dict[tuple[int, int], float]:
    return {(row, col): 1.0 for col, row in enumerate(CLEAN_ROWS)}


def _make_submission(
    db,
    pages_dir,
    batch,
    template_page,
    *,
    scan: bytes,
    status: SubmissionStatus = SubmissionStatus.SCANNED,
    candidate: str | None = None,
    created_at: datetime | None = None,
) -> Submission:
    submission = Submission(
        submission_batch_id=batch.id,
        matched_form_variant_id=template_page.form_variant_id,
        status=status,
        respondent_identifier=candidate,
    )
    if created_at is not None:
        submission.created_at = created_at
    db.add(submission)
    db.flush()
    page_id = uuid.uuid4()
    path = pages_dir / f"{page_id}.png"
    path.write_bytes(scan)
    db.add(
        SubmissionPage(
            id=page_id,
            submission_id=submission.id,
            form_variant_page_id=template_page.id,
            source_file_path=str(path),
            source_filename="scan.png",
            source_page_order=0,
        )
    )
    db.flush()
    return submission


@contextmanager
def _captured_sql(db) -> Iterator[list[tuple[str, object]]]:
    """Every statement emitted on the test connection meanwhile, with its
    parameters."""
    statements: list[tuple[str, object]] = []
    connection = db.connection()

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append((statement, parameters))

    event.listen(connection, "before_cursor_execute", _record)
    try:
        yield statements
    finally:
        event.remove(connection, "before_cursor_execute", _record)


def _mentions(parameters: object, submission: Submission) -> bool:
    """Whether *submission*'s id is among the bound *parameters* (one set, or
    several for an executemany)."""
    sets = parameters if isinstance(parameters, (list, tuple)) else [parameters]
    for values in sets:
        bound = values.values() if isinstance(values, dict) else values
        if any(str(value) == str(submission.id) for value in bound):
            return True
    return False


def _updates_of(statements, submission: Submission) -> list[str]:
    return [
        statement
        for statement, parameters in statements
        if statement.lstrip().upper().startswith("UPDATE SUBMISSIONS ")
        and _mentions(parameters, submission)
    ]


def _row_locks_of(statements, submission: Submission) -> list[str]:
    return [
        statement
        for statement, parameters in statements
        if "FROM submissions" in statement
        and "FOR UPDATE" in statement
        and _mentions(parameters, submission)
    ]


# ---------------------------------------------------------------------------
# Cause 1: the attempt time
# ---------------------------------------------------------------------------
def test_the_candidate_flush_does_not_write_a_submission_already_walked(
    production_session, pages_dir, batch, respondent_id_page
):
    """A SCANNED submission walked first (with a candidate, so the candidate
    rule leaves it alone), then one that receives its candidate: the flush the
    second needs must not write the first's row."""
    db = production_session
    walked_first = _make_submission(
        db, pages_dir, batch, respondent_id_page,
        scan=_draw_scan(_clean_marks()), candidate="FROM-RID", created_at=EARLIER,
    )
    receives = _make_submission(
        db, pages_dir, batch, respondent_id_page, scan=_draw_scan(_clean_marks())
    )

    with _captured_sql(db) as statements:
        summary = extraction_orchestrator_service.run_extraction_for_batch(db, batch.id)

    assert summary.candidates_set == 1
    assert receives.respondent_identifier == CLEAN_VALUE
    assert _updates_of(statements, walked_first) == []
    assert _row_locks_of(statements, walked_first) == []
    # The one it writes is still re-read under the lock before the write.
    assert _row_locks_of(statements, receives) != []


def test_the_attempt_time_lands_once_the_walk_ends(
    production_session, pages_dir, actor, batch, respondent_id_page
):
    """Still "the last time a run examined this submission": the same instant
    on every submission walked, never on a closed one."""
    db = production_session
    walked = [
        _make_submission(
            db, pages_dir, batch, respondent_id_page,
            scan=_draw_scan(_clean_marks()), created_at=EARLIER,
        ),
        _make_submission(
            db, pages_dir, batch, respondent_id_page,
            scan=_draw_scan(_clean_marks()), candidate="FROM-RID",
            created_at=EARLIER + timedelta(seconds=1),
        ),
    ]
    closed = _make_submission(
        db, pages_dir, batch, respondent_id_page,
        scan=_draw_scan(_clean_marks()), status=SubmissionStatus.REVIEWED,
    )

    run_batch_extraction(batch_id=batch.id, db=db, user=actor)
    for submission in (*walked, closed):
        db.refresh(submission)

    assert walked[0].extraction_attempted_at is not None
    assert walked[1].extraction_attempted_at == walked[0].extraction_attempted_at
    assert closed.extraction_attempted_at is None


# ---------------------------------------------------------------------------
# Cause 2: the candidate's row lock
# ---------------------------------------------------------------------------
def test_a_submission_with_no_respondent_id_is_not_row_locked(
    production_session, pages_dir, batch, variant
):
    """An anonymous questionnaire: no candidate and no respondent ID to give
    one, on every submission."""
    db = production_session
    blank_page = _make_template_page(db, variant, 2)
    submission = _make_submission(
        db, pages_dir, batch, blank_page, scan=_draw_scan({})
    )

    with _captured_sql(db) as statements:
        summary = extraction_orchestrator_service.run_extraction_for_batch(db, batch.id)

    assert summary.submissions_processed == 1
    assert summary.candidates_set == 0
    assert _row_locks_of(statements, submission) == []


def test_a_respondent_id_that_gives_no_candidate_does_not_row_lock_the_submission(
    production_session, pages_dir, batch, respondent_id_page
):
    """An exam: the respondent ID has a "?" and goes to review, so it gives no
    candidate — the submission an operator is about to open."""
    db = production_session
    marks = _clean_marks()
    del marks[(CLEAN_ROWS[2], 2)]
    submission = _make_submission(
        db, pages_dir, batch, respondent_id_page, scan=_draw_scan(marks)
    )

    with _captured_sql(db) as statements:
        summary = extraction_orchestrator_service.run_extraction_for_batch(db, batch.id)

    assert summary.results_created == 1
    assert summary.candidates_set == 0
    assert submission.respondent_identifier is None
    assert _row_locks_of(statements, submission) == []
