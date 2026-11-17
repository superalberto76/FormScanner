# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for RESPONDENT_IDENTIFIER extraction.

The respondent-ID-grid twin of ``test_matrix_extraction_integration.py``: real
synthetic scans generated with Pillow, laid out with the very geometry the PDF
export prints, read back by ``extract_respondent_identifier_result``. Unlike a
MATRIX question, there is no ``Validity`` axis here and the grid is always
read PER COLUMN, whatever ``MatrixSelectionRule`` the object happens to carry.

What this suite pins down:

  * **the normal grid** — several columns each with one clearly-inked digit
    give back the right ``raw_value``, the right ``digits``, and a
    ``confidence_score`` that is the arithmetic MEAN over every column;
  * **an empty column** — "?" in ``raw_value``, a 0.0 contribution to the
    mean, no entry in ``digits``; the result forces review through
    ``validity_forces_review``, however high the mean (test-kit case F10);
  * **an ambiguous column** — the highest-confidence cell wins, its
    ``col_index`` lands in ``ambiguous_columns``, and ``raw_value``/``digits``
    reflect the winner; the result forces review through
    ``validity_forces_review``, however high the confidence;
  * **labels, not indices** — ``raw_value`` uses ``MatrixRowDef.label``, and
    falls back to ``str(row_index)`` for a row with no definition;
  * **the degenerate grid** — zero rows or zero columns is not an error: an
    empty ``raw_value``, no digits, a NULL confidence, and review forced
    through ``validity_forces_review`` regardless of the threshold;
  * **review_required** — reflects ``confidence_score`` against the batch's
    effective threshold, via ``confidence_threshold_service``, exactly like
    every other extractor;
  * **preconditions** — an unmatched page, a non-MATRIX object, and a MATRIX
    with no RESPONDENT_IDENTIFIER role each raise rather than being handled by
    guesswork;
  * **result_type** — RESPONDENT_IDENTIFIER, never MATRIX;
  * **threshold recomputation** — an ambiguous, a degenerate and an
    incomplete (empty column) result stay
    ``review_required`` when the batch threshold is raised AND lowered, since
    the recomputation re-derives the flag from the two axes.

Skipped without TEST_DATABASE_URL, like the rest of tests/integration.
"""
from __future__ import annotations

import io
import uuid

import pytest
from PIL import Image, ImageDraw

from app.core.config import settings
from app.models.enums import (
    FormProjectStatus,
    FormProjectType,
    FormVariantStatus,
    MatrixSelectionRule,
    PageObjectRole,
    PageObjectType,
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
from app.models.form_variant_role_binding import FormVariantRoleBinding
from app.models.matrix_col_def import MatrixColDef
from app.models.matrix_object_config import MatrixObjectConfig
from app.models.matrix_row_def import MatrixRowDef
from app.models.page_object import PageObject
from app.models.question_object_config import QuestionObjectConfig
from app.models.submission import Submission
from app.models.submission_batch import SubmissionBatch
from app.models.submission_object_result import SubmissionObjectResult
from app.models.submission_page import SubmissionPage
from app.models.user import User
from app.services import confidence_threshold_service
from app.services import respondent_identifier_extraction_service as svc
from app.utils import question_layout

# The template page these tests author on: A4 @ 96 DPI, the export's default.
TEMPLATE_W = 794
TEMPLATE_H = 1123

# A respondent-ID grid: 10 digit rows ("0".."9"), 5 columns — tall enough that the
# cells are far apart compared with the 5px bubble radius.
OBJ_X = 60.0
OBJ_Y = 100.0
OBJ_W = 400.0
OBJ_H = 900.0

ROWS = 10
COLS = 5

WHITE = 255
BLACK = 0


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
def actor(db) -> User:
    u = User(
        email=f"rid-extract-{uuid.uuid4().hex}@example.com",
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
        name="RID extraction project",
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
def template_page(db, project, actor) -> FormVariantPage:
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
    page = FormVariantPage(
        form_variant_id=variant.id,
        page_index=1,
        width_px=TEMPLATE_W,
        height_px=TEMPLATE_H,
    )
    db.add(page)
    db.flush()
    return page


def _make_matrix(
    db,
    template_page: FormVariantPage,
    *,
    rows: int = ROWS,
    cols: int = COLS,
    row_labels: list[str] | None = None,
    role: PageObjectRole | None = PageObjectRole.RESPONDENT_IDENTIFIER,
) -> PageObject:
    """One authored MATRIX object, a respondent-ID grid by default.

    *row_labels* gives each ``MatrixRowDef`` an explicit label; ``None`` skips
    row definitions entirely (the fallback-to-index case), and a shorter list
    than *rows* leaves the remaining rows without one (a partial-fallback
    grid). ``MatrixSelectionRule`` is irrelevant for this extractor and always
    FREE — the grid is read per column regardless.
    """
    obj = PageObject(
        form_variant_page_id=template_page.id,
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
        rows_count=rows,
        cols_count=cols,
        selection_rule=MatrixSelectionRule.FREE,
    )
    db.add(config)
    db.flush()
    if row_labels is not None:
        for index, label in enumerate(row_labels):
            db.add(
                MatrixRowDef(
                    matrix_object_config_id=config.id,
                    order_index=index,
                    label=label,
                )
            )
        db.flush()
    for index in range(max(cols, 0)):
        db.add(
            MatrixColDef(
                matrix_object_config_id=config.id, order_index=index, label=f"C{index}"
            )
        )
    db.flush()
    if role is not None:
        db.add(
            FormVariantRoleBinding(
                form_variant_id=template_page.form_variant_id,
                page_object_id=obj.id,
                role=role,
            )
        )
        db.flush()
    db.refresh(obj)
    return obj


def _make_batch(db, project, actor, *, threshold: float | None = None):
    batch = SubmissionBatch(
        form_project_id=project.id,
        name="Batch 1",
        created_by_user_id=actor.id,
        confidence_threshold=threshold,
    )
    db.add(batch)
    db.flush()
    return batch


def _cell_center(
    row: int, col: int, *, rows: int = ROWS, cols: int = COLS
) -> tuple[float, float]:
    """Where the export prints the (row, col) bubble, in template space.

    Written out from the raw formula (same as
    ``test_matrix_extraction_integration._cell_center``): borrowing the
    extractor's own placement would make every detection test pass by
    construction.
    """
    label_w = min(OBJ_W * 0.25, 120.0)
    grid_x = OBJ_X + label_w
    grid_y = OBJ_Y + 16 + 16  # LABEL_H + HEADER_H
    cell_w = max(OBJ_W - label_w, 1.0) / cols
    cell_h = max((OBJ_Y + OBJ_H) - grid_y, 1.0) / rows
    return (
        grid_x + col * cell_w + cell_w / 2,
        grid_y + row * cell_h + cell_h / 2,
    )


def _draw_scan(
    filled: dict[tuple[int, int], float] | None = None,
    *,
    outlined: tuple[tuple[int, int], ...] = (),
    rows: int = ROWS,
    cols: int = COLS,
    scale: float = 4.0,
) -> bytes:
    """A synthetic scan of the template page, as PNG bytes — see
    ``test_matrix_extraction_integration._draw_scan`` for the fraction/outline
    contract."""
    filled = filled or {}
    img = Image.new("L", (int(TEMPLATE_W * scale), int(TEMPLATE_H * scale)), WHITE)
    draw = ImageDraw.Draw(img)
    printed_radius = question_layout.CIRCLE_R * scale

    for row, col in outlined:
        cx, cy = _cell_center(row, col, rows=rows, cols=cols)
        cx, cy = cx * scale, cy * scale
        draw.ellipse(
            [cx - printed_radius, cy - printed_radius,
             cx + printed_radius, cy + printed_radius],
            outline=BLACK,
            width=max(int(scale / 2), 1),
        )
    for (row, col), fraction in filled.items():
        cx, cy = _cell_center(row, col, rows=rows, cols=cols)
        cx, cy = cx * scale, cy * scale
        r = printed_radius * fraction
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=BLACK)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _all_cells(*, rows: int = ROWS, cols: int = COLS) -> tuple[tuple[int, int], ...]:
    return tuple((r, c) for r in range(rows) for c in range(cols))


def _make_scanned_page(
    db,
    pages_dir,
    batch,
    template_page: FormVariantPage,
    *,
    scan: bytes,
    matched: bool = True,
) -> SubmissionPage:
    submission = Submission(
        submission_batch_id=batch.id,
        matched_form_variant_id=template_page.form_variant_id,
        status=SubmissionStatus.MATCHED,
    )
    db.add(submission)
    db.flush()
    page_id = uuid.uuid4()
    path = pages_dir / f"{page_id}.png"
    path.write_bytes(scan)
    page = SubmissionPage(
        id=page_id,
        submission_id=submission.id,
        form_variant_page_id=template_page.id if matched else None,
        source_file_path=str(path),
        source_filename="scan.png",
        source_page_order=0,
    )
    db.add(page)
    db.flush()
    return page


DIGIT_LABELS = [str(d) for d in range(10)]


# ---------------------------------------------------------------------------
# The normal grid
# ---------------------------------------------------------------------------
def test_a_normal_grid_reads_the_inked_digit_of_every_column(
    db, pages_dir, project, actor, template_page
):
    obj = _make_matrix(db, template_page, row_labels=DIGIT_LABELS)
    batch = _make_batch(db, project, actor, threshold=0.3)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(
            {(3, 0): 1.0, (7, 1): 1.0, (0, 2): 1.0, (9, 3): 1.0, (5, 4): 1.0},
            outlined=_all_cells(),
        ),
    )

    result = svc.extract_respondent_identifier_result(db, page, obj, batch)
    payload = result.detected_payload_json

    assert payload["raw_value"] == "37095"
    assert [d["col_index"] for d in payload["digits"]] == [0, 1, 2, 3, 4]
    assert [d["label"] for d in payload["digits"]] == ["3", "7", "0", "9", "5"]
    assert payload["ambiguous_columns"] == []
    assert result.confidence_score == pytest.approx(
        sum(d["confidence"] for d in payload["digits"]) / COLS
    )
    assert result.result_type is SubmissionResultType.RESPONDENT_IDENTIFIER


# ---------------------------------------------------------------------------
# An empty column
# ---------------------------------------------------------------------------
def test_an_empty_column_is_a_question_mark_and_contributes_zero(
    db, pages_dir, project, actor, template_page
):
    obj = _make_matrix(db, template_page, row_labels=DIGIT_LABELS)
    batch = _make_batch(db, project, actor, threshold=0.1)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        # Column 2 is left entirely blank.
        scan=_draw_scan({(1, 0): 1.0, (4, 1): 1.0, (8, 3): 1.0, (6, 4): 1.0},
                         outlined=_all_cells()),
    )

    result = svc.extract_respondent_identifier_result(db, page, obj, batch)
    payload = result.detected_payload_json

    assert payload["raw_value"][2] == "?"
    assert [d["col_index"] for d in payload["digits"]] == [0, 1, 3, 4]
    non_empty = [d["confidence"] for d in payload["digits"]]
    assert result.confidence_score == pytest.approx(sum(non_empty) / COLS)


@pytest.mark.parametrize("threshold", [None, 0.0], ids=["default", "zero"])
def test_an_empty_column_forces_review_even_above_the_threshold(
    db, pages_dir, project, actor, template_page, threshold
):
    """Test-kit case F10: one blank column out of five, the others cleanly inked
    — the mean stays above the threshold (the global default, or 0.0), so the
    confidence axis alone would leave out of review an identifier that cannot
    be confirmed."""
    obj = _make_matrix(db, template_page, row_labels=DIGIT_LABELS)
    batch = _make_batch(db, project, actor, threshold=threshold)
    marks = {(i, i): 1.0 for i in range(COLS - 1)}
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(marks, outlined=_all_cells()),
    )

    result = svc.extract_respondent_identifier_result(db, page, obj, batch)

    assert result.detected_payload_json["raw_value"].endswith("?")
    assert result.detected_payload_json["ambiguous_columns"] == []
    assert result.confidence_score >= confidence_threshold_service.effective_threshold(
        batch
    )
    assert result.validity_forces_review is True
    assert result.review_required is True


# ---------------------------------------------------------------------------
# An ambiguous column
# ---------------------------------------------------------------------------
def test_an_ambiguous_column_picks_the_higher_confidence_reading(
    db, pages_dir, project, actor, template_page
):
    """Two cells inked in the same column: the fainter one (a smaller filled
    fraction of the printed radius, hence a lower fill ratio) must lose to the
    one filled to the edge, and the column must be flagged."""
    obj = _make_matrix(db, template_page, row_labels=DIGIT_LABELS)
    batch = _make_batch(db, project, actor, threshold=0.1)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan({(2, 0): 1.0, (6, 0): 0.75}, outlined=_all_cells()),
    )

    result = svc.extract_respondent_identifier_result(db, page, obj, batch)
    payload = result.detected_payload_json

    assert payload["raw_value"][0] == "2"
    assert payload["digits"][0]["row_index"] == 2
    assert payload["ambiguous_columns"] == [0]
    assert result.validity_forces_review is True
    assert result.review_required is True


def test_an_ambiguous_column_forces_review_even_with_high_confidence(
    db, pages_dir, project, actor, template_page
):
    """Every column cleanly inked, plus a second clean mark in column 0: the
    mean confidence is far above the threshold, so the confidence axis alone
    would auto-accept an identifier one digit of which the system chose."""
    obj = _make_matrix(db, template_page, row_labels=DIGIT_LABELS)
    batch = _make_batch(db, project, actor, threshold=0.1)
    marks = {(i, i): 1.0 for i in range(COLS)}
    marks[(8, 0)] = 1.0
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(marks, outlined=_all_cells()),
    )

    result = svc.extract_respondent_identifier_result(db, page, obj, batch)

    assert result.detected_payload_json["ambiguous_columns"] == [0]
    assert result.confidence_score > 0.1
    assert result.validity_forces_review is True
    assert result.review_required is True


# ---------------------------------------------------------------------------
# Labels, not indices
# ---------------------------------------------------------------------------
def test_raw_value_uses_the_row_label_not_the_row_index(
    db, pages_dir, project, actor, template_page
):
    """Labels deliberately NOT equal to ``str(row_index)``, so a raw_value
    that happened to match the index by coincidence cannot pass this test."""
    letters = [chr(ord("A") + i) for i in range(ROWS)]  # "A".."J"
    obj = _make_matrix(db, template_page, row_labels=letters)
    batch = _make_batch(db, project, actor, threshold=0.1)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan({(0, 0): 1.0}, outlined=_all_cells()),
    )

    result = svc.extract_respondent_identifier_result(db, page, obj, batch)
    payload = result.detected_payload_json

    assert payload["raw_value"][0] == "A"
    assert payload["digits"][0]["label"] == "A"


def test_a_row_without_a_definition_falls_back_to_its_index(
    db, pages_dir, project, actor, template_page
):
    """Only the first three rows get an explicit label; row 5 must fall back
    to ``str(5)``, the same tolerance ``_render_matrix`` applies."""
    obj = _make_matrix(db, template_page, row_labels=["X", "Y", "Z"])
    batch = _make_batch(db, project, actor, threshold=0.1)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan({(5, 0): 1.0}, outlined=_all_cells()),
    )

    result = svc.extract_respondent_identifier_result(db, page, obj, batch)
    payload = result.detected_payload_json

    assert payload["raw_value"][0] == "5"
    assert payload["digits"][0]["label"] == "5"


def test_no_row_definitions_at_all_still_reads_by_index(
    db, pages_dir, project, actor, template_page
):
    obj = _make_matrix(db, template_page, row_labels=None)
    batch = _make_batch(db, project, actor, threshold=0.1)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan({(9, 4): 1.0}, outlined=_all_cells()),
    )

    result = svc.extract_respondent_identifier_result(db, page, obj, batch)
    payload = result.detected_payload_json

    assert payload["raw_value"][4] == "9"


# ---------------------------------------------------------------------------
# The degenerate grid
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("rows, cols", [(0, 5), (10, 0), (0, 0), (-1, 5), (10, -2)])
def test_a_degenerate_grid_reads_as_empty_not_an_error(
    db, pages_dir, project, actor, template_page, rows, cols
):
    obj = _make_matrix(db, template_page, rows=rows, cols=cols, row_labels=None)
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(db, pages_dir, batch, template_page, scan=_draw_scan())

    result = svc.extract_respondent_identifier_result(db, page, obj, batch)

    assert result.detected_payload_json == {
        "raw_value": "", "digits": [], "ambiguous_columns": []
    }
    assert result.confidence_score is None
    # Forced on the axis, independently of the (absent) confidence — a
    # structurally broken grid always needs a human.
    assert result.validity_forces_review is True
    assert result.review_required is True


def test_the_degenerate_review_flag_is_forced_even_under_a_lenient_threshold(
    db, pages_dir, project, actor, template_page
):
    """The forcing is NOT a side effect of a NULL confidence never clearing a
    threshold: it must hold even when the batch's threshold is 0.0, which
    would otherwise never flag a NULL confidence."""
    obj = _make_matrix(db, template_page, rows=0, cols=5, row_labels=None)
    batch = _make_batch(db, project, actor, threshold=0.0)
    page = _make_scanned_page(db, pages_dir, batch, template_page, scan=_draw_scan())

    result = svc.extract_respondent_identifier_result(db, page, obj, batch)

    assert result.validity_forces_review is True
    assert result.review_required is True


# ---------------------------------------------------------------------------
# review_required vs the batch's effective threshold
# ---------------------------------------------------------------------------
def test_review_required_follows_the_batch_threshold(
    db, pages_dir, project, actor, template_page
):
    obj = _make_matrix(db, template_page, row_labels=DIGIT_LABELS)
    batch = _make_batch(db, project, actor, threshold=0.9)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        # Every column answered, but only to 80% of the printed radius —
        # comfortably below a 0.9 threshold.
        scan=_draw_scan(
            {(i, i): 0.8 for i in range(COLS)}, outlined=_all_cells()
        ),
    )

    result = svc.extract_respondent_identifier_result(db, page, obj, batch)

    assert result.confidence_score < 0.9
    assert result.review_required is True


def test_review_required_is_false_above_threshold(
    db, pages_dir, project, actor, template_page
):
    obj = _make_matrix(db, template_page, row_labels=DIGIT_LABELS)
    batch = _make_batch(db, project, actor, threshold=0.1)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan({(i, i): 1.0 for i in range(COLS)}, outlined=_all_cells()),
    )

    result = svc.extract_respondent_identifier_result(db, page, obj, batch)

    assert result.confidence_score > 0.1
    assert result.detected_payload_json["ambiguous_columns"] == []
    assert result.validity_forces_review is False
    assert result.review_required is False


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------
def test_a_matrix_with_no_role_is_refused_outright(
    db, pages_dir, project, actor, template_page
):
    """The defensive guard: a plain evaluable MATRIX must never be read by
    this function, or a dispatch bug upstream would silently attribute a
    candidate's answers to their identity."""
    obj = _make_matrix(db, template_page, row_labels=DIGIT_LABELS, role=None)
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page, scan=_draw_scan({(0, 0): 1.0})
    )

    with pytest.raises(ValueError, match="RESPONDENT_IDENTIFIER"):
        svc.extract_respondent_identifier_result(db, page, obj, batch)


def test_an_unmatched_page_raises_rather_than_guessing(
    db, pages_dir, project, actor, template_page
):
    obj = _make_matrix(db, template_page, row_labels=DIGIT_LABELS)
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan({(0, 0): 1.0}), matched=False,
    )

    with pytest.raises(ValueError, match="not matched"):
        svc.extract_respondent_identifier_result(db, page, obj, batch)


def test_a_non_matrix_object_raises(db, pages_dir, project, actor, template_page):
    obj = PageObject(
        form_variant_page_id=template_page.id,
        object_type=PageObjectType.QUESTION,
        x=OBJ_X, y=OBJ_Y, width=OBJ_W, height=OBJ_H,
    )
    db.add(obj)
    db.flush()
    db.add(
        QuestionObjectConfig(
            page_object_id=obj.id,
            question_type=QuestionType.SINGLE_CHOICE,
            question_text="Which one?",
        )
    )
    db.flush()
    db.refresh(obj)
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(db, pages_dir, batch, template_page, scan=_draw_scan())

    with pytest.raises(ValueError, match="matrix_config"):
        svc.extract_respondent_identifier_result(db, page, obj, batch)


# ---------------------------------------------------------------------------
# The staged row itself
# ---------------------------------------------------------------------------
def test_the_result_is_typed_respondent_identifier_and_staged_not_committed(
    db, pages_dir, project, actor, template_page
):
    obj = _make_matrix(db, template_page, row_labels=DIGIT_LABELS)
    batch = _make_batch(db, project, actor, threshold=0.3)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan({(0, 0): 1.0}, outlined=_all_cells()),
    )

    result = svc.extract_respondent_identifier_result(db, page, obj, batch)

    assert result.result_type is SubmissionResultType.RESPONDENT_IDENTIFIER
    assert result.page_object_id == obj.id
    assert result.submission_page_id == page.id
    assert result in db.new
    assert isinstance(result, SubmissionObjectResult)


def test_the_reading_survives_a_flush_as_plain_json(
    db, pages_dir, project, actor, template_page
):
    obj = _make_matrix(db, template_page, row_labels=DIGIT_LABELS)
    batch = _make_batch(db, project, actor, threshold=0.3)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan({(4, 1): 1.0}, outlined=_all_cells()),
    )

    result = svc.extract_respondent_identifier_result(db, page, obj, batch)
    db.flush()
    db.refresh(result)

    assert result.detected_payload_json["digits"][0] == {
        "col_index": 1,
        "row_index": 4,
        "label": "4",
        "confidence": pytest.approx(result.confidence_score * COLS),
    }


# ---------------------------------------------------------------------------
# Threshold recomputation keeps the forced cases in review
# ---------------------------------------------------------------------------
def _ambiguous_result(db, pages_dir, project, actor, template_page, batch):
    obj = _make_matrix(db, template_page, row_labels=DIGIT_LABELS)
    marks = {(i, i): 1.0 for i in range(COLS)}
    marks[(8, 0)] = 1.0
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(marks, outlined=_all_cells()),
    )
    return svc.extract_respondent_identifier_result(db, page, obj, batch)


def _degenerate_result(db, pages_dir, project, actor, template_page, batch):
    obj = _make_matrix(db, template_page, rows=0, cols=5, row_labels=None)
    page = _make_scanned_page(db, pages_dir, batch, template_page, scan=_draw_scan())
    return svc.extract_respondent_identifier_result(db, page, obj, batch)


def _missing_digit_result(db, pages_dir, project, actor, template_page, batch):
    obj = _make_matrix(db, template_page, row_labels=DIGIT_LABELS)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(
            {(i, i): 1.0 for i in range(COLS - 1)}, outlined=_all_cells()
        ),
    )
    return svc.extract_respondent_identifier_result(db, page, obj, batch)


@pytest.mark.parametrize(
    "make",
    [_ambiguous_result, _degenerate_result, _missing_digit_result],
    ids=["ambiguous", "degenerate", "missing-digit"],
)
def test_a_forced_result_stays_in_review_whichever_way_the_threshold_moves(
    db, pages_dir, project, actor, template_page, make
):
    batch = _make_batch(db, project, actor, threshold=0.5)
    result = make(db, pages_dir, project, actor, template_page, batch)
    db.flush()
    assert result.review_required is True

    for threshold in (0.99, 0.0):
        batch.confidence_threshold = threshold
        confidence_threshold_service.recompute_review_required_for_batch(
            db, batch.id
        )
        db.flush()
        db.refresh(result)
        assert result.validity_forces_review is True
        assert result.review_required is True, threshold
