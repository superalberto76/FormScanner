# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for MATRIX extraction.

The full chain on REAL synthetic scans generated with Pillow, in the style of
the QUESTION suite: an authored MATRIX is laid out with the very geometry the
PDF export prints, a scan of that page is drawn with some cells blackened, and
``extract_matrix_result`` has to read back exactly which ones — and say what
they mean under the object's selection rule.

What this suite pins down:

  * **the per-group verdicts** — zero / one / several marks in a group under
    every one of the five selection rules. NO_ANSWER for an empty group under
    ALL of them, FREE included; TOO_MANY_ANSWERS only where the rule allows one
    cell per group;
  * **the two orientations** — ONE_PER_ROW / MANY_PER_ROW / FREE report ``rows``
    and leave ``cols`` empty; ONE_PER_COLUMN / MANY_PER_COLUMN do the mirror
    image, referencing their cells by ``row_index``. The SAME inked sheet read
    under a row rule and under a column rule gives the two groupings of one set
    of readings;
  * **the two object-level axes** — ``confidence_score`` is the MINIMUM over
    every selected cell of every group (NULL when nothing was marked), and
    ``validity_forces_review`` is True as soon as ONE group is not VALID,
    however many perfect ones surround it;
  * **preconditions** — an unmatched page, a non-MATRIX object, and a MATRIX
    holding the RESPONDENT_IDENTIFIER role (a respondent-ID grid) each
    raise rather than being handled by guesswork;
  * **the degenerate matrix** — zero rows or zero columns is not an error: the
    export prints no grid, so there is nothing to read.

The cell centres are recomputed here from the raw formula rather than imported
from ``matrix_layout``: a test that placed its ink using the extractor's own
placement could not catch the extractor placing it wrongly.

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
    Validity,
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
from app.services import matrix_extraction_service as svc
from app.utils import question_layout


# The template page these tests author on: A4 @ 96 DPI, the export's default.
TEMPLATE_W = 794
TEMPLATE_H = 1123

# The MATRIX object's geometry. Wide and tall enough that the cells of the
# default 3x4 grid are far apart compared with the 5px bubble radius, so one
# cell's ink can never be read as its neighbour's.
OBJ_X = 60.0
OBJ_Y = 120.0
OBJ_W = 400.0
OBJ_H = 300.0

ROWS = 3
COLS = 4

WHITE = 255
BLACK = 0


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


@pytest.fixture
def pages_dir(tmp_path, monkeypatch):
    """Redirect submission-page storage into the test's tmp_path.

    ``read_submission_page_image`` validates every path against this directory,
    so a scan written anywhere else would be refused.
    """
    target = tmp_path / "submission_pages"
    target.mkdir()
    monkeypatch.setattr(settings, "submission_pages_dir", str(target))
    return target


@pytest.fixture
def actor(db) -> User:
    u = User(
        email=f"matrix-extract-{uuid.uuid4().hex}@example.com",
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
        name="Matrix extraction project",
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
    rule: MatrixSelectionRule = MatrixSelectionRule.ONE_PER_ROW,
    rows: int = ROWS,
    cols: int = COLS,
    with_definitions: bool = True,
    role: PageObjectRole | None = None,
) -> PageObject:
    """One authored MATRIX object, optionally holding a scanning role.

    *with_definitions* controls whether ``MatrixRowDef`` / ``MatrixColDef``
    label rows exist. They are LABELS: the export prints a grid of
    ``rows_count x cols_count`` bubbles either way, so extraction must too.
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
        selection_rule=rule,
    )
    db.add(config)
    db.flush()
    if with_definitions:
        for index in range(max(rows, 0)):
            db.add(
                MatrixRowDef(
                    matrix_object_config_id=config.id,
                    order_index=index,
                    label=f"Riga {index}",
                )
            )
        for index in range(max(cols, 0)):
            db.add(
                MatrixColDef(
                    matrix_object_config_id=config.id,
                    order_index=index,
                    label=f"Colonna {index}",
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

    The raw formula, written out here rather than imported from
    ``app.utils.matrix_layout``: this is what the ink is placed with, and
    borrowing the extractor's own placement would make every detection test
    pass by construction.
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
    scale: float = 4.0,
) -> bytes:
    """A synthetic scan of the template page, as PNG bytes.

    *filled* maps a (row, col) cell to the fraction of the printed radius to
    blacken — 1.0 is a bubble filled to the edge, a smaller value one filled
    less thoroughly, which is how these tests produce DIFFERENT confidences for
    different cells (the ratio measures AREA, so a fraction f reads ≈ f²).
    *outlined* draws the empty circle the export prints for an unmarked cell, so
    "not selected" is tested against a real outline and not against blank paper.
    """
    filled = filled or {}
    img = Image.new("L", (int(TEMPLATE_W * scale), int(TEMPLATE_H * scale)), WHITE)
    draw = ImageDraw.Draw(img)
    printed_radius = question_layout.CIRCLE_R * scale

    for row, col in outlined:
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
    """A matched SubmissionPage whose stored image is *scan*."""
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


def _rows(result) -> list[dict]:
    return result.detected_payload_json["rows"]


def _cols(result) -> list[dict]:
    return result.detected_payload_json["cols"]


def _validities(groups: list[dict]) -> list[str]:
    return [g["validity"] for g in groups]


# ---------------------------------------------------------------------------
# ONE_PER_ROW — the three validity outcomes, row by row
# ---------------------------------------------------------------------------
def test_one_per_row_reads_each_row_as_its_own_mini_question(
    db, pages_dir, project, actor, template_page
):
    """The principle of the functional document, on one sheet: row 0 answered,
    row 1 skipped, row 2 answered twice. Three verdicts on one object, because
    validity is per GROUP and the matrix as a whole carries none."""
    obj = _make_matrix(db, template_page, rule=MatrixSelectionRule.ONE_PER_ROW)
    batch = _make_batch(db, project, actor, threshold=0.3)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(
            {(0, 1): 1.0, (2, 0): 1.0, (2, 3): 1.0}, outlined=_all_cells()
        ),
    )

    result = svc.extract_matrix_result(db, page, obj, batch)

    assert _validities(_rows(result)) == [
        Validity.VALID.value,
        Validity.NO_ANSWER.value,
        Validity.TOO_MANY_ANSWERS.value,
    ]
    assert [c["col_index"] for c in _rows(result)[0]["selected_cells"]] == [1]
    assert _rows(result)[1]["selected_cells"] == []
    assert [c["col_index"] for c in _rows(result)[2]["selected_cells"]] == [0, 3]


def test_an_untouched_matrix_is_no_answer_in_every_row(
    db, pages_dir, project, actor, template_page
):
    """Every bubble printed and none filled — the object a candidate skipped.
    Every row is still reported: a skipped row and a row that was never printed
    are different facts."""
    obj = _make_matrix(db, template_page, rule=MatrixSelectionRule.ONE_PER_ROW)
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(outlined=_all_cells()),
    )

    result = svc.extract_matrix_result(db, page, obj, batch)

    assert len(_rows(result)) == ROWS
    assert _validities(_rows(result)) == [Validity.NO_ANSWER.value] * ROWS
    # NULL, not 0.0: there is no reading to be confident about.
    assert result.confidence_score is None
    assert result.validity_forces_review is True


def test_the_cell_read_is_the_cell_that_was_inked(
    db, pages_dir, project, actor, template_page
):
    """No off-by-one in either dimension: every single cell of the grid, one at
    a time, has to come back as itself."""
    obj = _make_matrix(db, template_page, rule=MatrixSelectionRule.MANY_PER_ROW)
    batch = _make_batch(db, project, actor)

    for row in range(ROWS):
        for col in range(COLS):
            page = _make_scanned_page(
                db, pages_dir, batch, template_page,
                scan=_draw_scan({(row, col): 1.0}, outlined=_all_cells()),
            )
            result = svc.extract_matrix_result(db, page, obj, batch)
            marked = [
                (g["row_index"], c["col_index"])
                for g in _rows(result)
                for c in g["selected_cells"]
            ]
            assert marked == [(row, col)]


# ---------------------------------------------------------------------------
# MANY_PER_ROW and FREE — no upper limit, but zero is still NO_ANSWER
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "rule", [MatrixSelectionRule.MANY_PER_ROW, MatrixSelectionRule.FREE]
)
def test_several_marks_in_a_row_are_valid_under_a_multi_selection_rule(
    db, pages_dir, project, actor, template_page, rule
):
    """FREE behaves exactly as MANY_PER_ROW here — three marks in one row are
    the answer, not a violation."""
    obj = _make_matrix(db, template_page, rule=rule)
    batch = _make_batch(db, project, actor, threshold=0.3)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(
            {(0, 0): 1.0, (0, 1): 1.0, (0, 2): 1.0}, outlined=_all_cells()
        ),
    )

    result = svc.extract_matrix_result(db, page, obj, batch)

    assert _rows(result)[0]["validity"] == Validity.VALID.value
    assert len(_rows(result)[0]["selected_cells"]) == 3
    # …and the untouched rows are still NO_ANSWER, which is what makes the
    # object need a review even under the most permissive rule there is.
    assert _validities(_rows(result))[1:] == [Validity.NO_ANSWER.value] * (ROWS - 1)
    assert result.validity_forces_review is True


def test_free_does_not_mean_an_empty_group_is_an_answer(
    db, pages_dir, project, actor, template_page
):
    """The reading of FREE, fixed deliberately: "no constraint" caps how MANY
    cells may be marked, it does not make a blank group valid."""
    obj = _make_matrix(db, template_page, rule=MatrixSelectionRule.FREE)
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(outlined=_all_cells()),
    )

    result = svc.extract_matrix_result(db, page, obj, batch)

    assert _validities(_rows(result)) == [Validity.NO_ANSWER.value] * ROWS
    assert result.validity_forces_review is True


# ---------------------------------------------------------------------------
# The column-oriented rules — the mirror image
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "rule", [MatrixSelectionRule.ONE_PER_COLUMN, MatrixSelectionRule.MANY_PER_COLUMN]
)
def test_a_column_rule_reports_columns_and_leaves_the_rows_empty(
    db, pages_dir, project, actor, template_page, rule
):
    """Which of the two lists carries the reading is what says which rule the
    object was authored with — exactly one of them is ever populated."""
    obj = _make_matrix(db, template_page, rule=rule)
    batch = _make_batch(db, project, actor, threshold=0.3)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan({(1, 2): 1.0}, outlined=_all_cells()),
    )

    result = svc.extract_matrix_result(db, page, obj, batch)

    assert _rows(result) == []
    assert [g["col_index"] for g in _cols(result)] == list(range(COLS))
    # The cell of a column group is referenced by its ROW.
    assert [c["row_index"] for c in _cols(result)[2]["selected_cells"]] == [1]
    assert _cols(result)[2]["validity"] == Validity.VALID.value
    assert _validities(_cols(result))[:2] == [Validity.NO_ANSWER.value] * 2


def test_one_per_column_calls_two_marks_in_a_column_too_many(
    db, pages_dir, project, actor, template_page
):
    obj = _make_matrix(db, template_page, rule=MatrixSelectionRule.ONE_PER_COLUMN)
    batch = _make_batch(db, project, actor, threshold=0.3)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan({(0, 0): 1.0, (2, 0): 1.0}, outlined=_all_cells()),
    )

    result = svc.extract_matrix_result(db, page, obj, batch)

    assert _cols(result)[0]["validity"] == Validity.TOO_MANY_ANSWERS.value
    assert [c["row_index"] for c in _cols(result)[0]["selected_cells"]] == [0, 2]


def test_many_per_column_accepts_the_very_same_sheet(
    db, pages_dir, project, actor, template_page
):
    """The rule, not the ink, is what decides: two marks down one column are a
    violation under ONE_PER_COLUMN and the answer under MANY_PER_COLUMN."""
    obj = _make_matrix(db, template_page, rule=MatrixSelectionRule.MANY_PER_COLUMN)
    batch = _make_batch(db, project, actor, threshold=0.3)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan({(0, 0): 1.0, (2, 0): 1.0}, outlined=_all_cells()),
    )

    result = svc.extract_matrix_result(db, page, obj, batch)

    assert _cols(result)[0]["validity"] == Validity.VALID.value


def test_the_same_inked_sheet_read_under_both_orientations(
    db, pages_dir, project, actor, template_page
):
    """The symmetry in full: one scan, two objects differing only in their rule,
    and the two groupings of the same three readings."""
    scan = _draw_scan(
        {(0, 0): 1.0, (0, 1): 1.0, (2, 1): 1.0}, outlined=_all_cells()
    )
    by_row = _make_matrix(db, template_page, rule=MatrixSelectionRule.MANY_PER_ROW)
    by_col = _make_matrix(db, template_page, rule=MatrixSelectionRule.MANY_PER_COLUMN)
    batch = _make_batch(db, project, actor, threshold=0.3)
    page_a = _make_scanned_page(db, pages_dir, batch, template_page, scan=scan)
    page_b = _make_scanned_page(db, pages_dir, batch, template_page, scan=scan)

    row_result = svc.extract_matrix_result(db, page_a, by_row, batch)
    col_result = svc.extract_matrix_result(db, page_b, by_col, batch)

    assert [
        (g["row_index"], c["col_index"])
        for g in _rows(row_result)
        for c in g["selected_cells"]
    ] == [(0, 0), (0, 1), (2, 1)]
    assert [
        (c["row_index"], g["col_index"])
        for g in _cols(col_result)
        for c in g["selected_cells"]
    ] == [(0, 0), (0, 1), (2, 1)]


# ---------------------------------------------------------------------------
# confidence_score — the minimum over the WHOLE object
# ---------------------------------------------------------------------------
def test_the_confidence_is_the_least_sure_cell_of_the_whole_matrix(
    db, pages_dir, project, actor, template_page
):
    """Not per row: the object is ONE result, and it is only as sure as its
    least sure bubble — wherever in the grid that bubble happens to be."""
    obj = _make_matrix(db, template_page, rule=MatrixSelectionRule.MANY_PER_ROW)
    batch = _make_batch(db, project, actor, threshold=0.1)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(
            # The faintest mark is in the LAST row, so a per-row minimum, or a
            # first-seen one, would report the wrong number.
            {(0, 0): 1.0, (1, 1): 0.95, (2, 2): 0.8},
            outlined=_all_cells(),
        ),
    )

    result = svc.extract_matrix_result(db, page, obj, batch)

    confidences = [
        c["confidence"] for g in _rows(result) for c in g["selected_cells"]
    ]
    assert len(confidences) == 3
    assert result.confidence_score == pytest.approx(min(confidences))
    # The ratio is a measurement, not a verdict: the cell filled to 80% of the
    # printed radius is read as strictly less sure than the ones filled to the
    # edge. (Its reading is above 0.8² because the printed OUTLINE is inside the
    # sampling disc too — the ink of a real, partly-filled bubble.)
    assert result.confidence_score < max(confidences)


def test_the_confidence_spans_the_column_groups_too(
    db, pages_dir, project, actor, template_page
):
    obj = _make_matrix(db, template_page, rule=MatrixSelectionRule.MANY_PER_COLUMN)
    batch = _make_batch(db, project, actor, threshold=0.1)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan({(0, 0): 1.0, (1, 3): 0.8}, outlined=_all_cells()),
    )

    result = svc.extract_matrix_result(db, page, obj, batch)

    confidences = [
        c["confidence"] for g in _cols(result) for c in g["selected_cells"]
    ]
    assert result.confidence_score == pytest.approx(min(confidences))


def test_nothing_marked_anywhere_leaves_the_confidence_null(
    db, pages_dir, project, actor, template_page
):
    """NULL is not 0.0: an unanswered object is handled by Validity, and
    claiming zero confidence would double-count the same fact on the axis meant
    to stay independent of it."""
    obj = _make_matrix(db, template_page, rule=MatrixSelectionRule.MANY_PER_COLUMN)
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page, scan=_draw_scan(outlined=_all_cells())
    )

    result = svc.extract_matrix_result(db, page, obj, batch)

    assert result.confidence_score is None


# ---------------------------------------------------------------------------
# validity_forces_review — one bad group is enough
# ---------------------------------------------------------------------------
def test_one_invalid_row_out_of_many_forces_review_of_the_whole_object(
    db, pages_dir, project, actor, template_page
):
    """The rule for MATRIX, stated in its own terms:
    "review_required at the level of the whole MATRIX object is true if at
    least one row has review_required true"."""
    obj = _make_matrix(db, template_page, rule=MatrixSelectionRule.ONE_PER_ROW)
    batch = _make_batch(db, project, actor, threshold=0.1)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(
            # Rows 0 and 1 answered cleanly; row 2 answered twice.
            {(0, 0): 1.0, (1, 1): 1.0, (2, 2): 1.0, (2, 3): 1.0},
            outlined=_all_cells(),
        ),
    )

    result = svc.extract_matrix_result(db, page, obj, batch)

    assert _validities(_rows(result)) == [
        Validity.VALID.value, Validity.VALID.value, Validity.TOO_MANY_ANSWERS.value
    ]
    assert result.validity_forces_review is True
    # …and it lands on review_required even though the ink itself was perfect.
    assert result.confidence_score > 0.9
    assert result.review_required is True


def test_one_invalid_column_out_of_many_forces_review_too(
    db, pages_dir, project, actor, template_page
):
    obj = _make_matrix(db, template_page, rule=MatrixSelectionRule.MANY_PER_COLUMN)
    batch = _make_batch(db, project, actor, threshold=0.1)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        # Every column answered except column 3.
        scan=_draw_scan(
            {(0, 0): 1.0, (0, 1): 1.0, (0, 2): 1.0}, outlined=_all_cells()
        ),
    )

    result = svc.extract_matrix_result(db, page, obj, batch)

    assert _validities(_cols(result)) == [
        Validity.VALID.value, Validity.VALID.value, Validity.VALID.value,
        Validity.NO_ANSWER.value,
    ]
    assert result.validity_forces_review is True


def test_every_group_valid_leaves_the_object_alone(
    db, pages_dir, project, actor, template_page
):
    obj = _make_matrix(db, template_page, rule=MatrixSelectionRule.ONE_PER_ROW)
    batch = _make_batch(db, project, actor, threshold=0.1)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(
            {(0, 0): 1.0, (1, 1): 1.0, (2, 2): 1.0}, outlined=_all_cells()
        ),
    )

    result = svc.extract_matrix_result(db, page, obj, batch)

    assert result.validity_forces_review is False
    assert result.review_required is False


def test_a_faint_but_valid_matrix_still_goes_to_review_on_confidence(
    db, pages_dir, project, actor, template_page
):
    """The confidence axis, unchanged for MATRIX: a perfectly legal set
    of marks read faintly needs a human just the same."""
    obj = _make_matrix(db, template_page, rule=MatrixSelectionRule.ONE_PER_ROW)
    batch = _make_batch(db, project, actor, threshold=0.9)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan(
            {(0, 0): 1.0, (1, 1): 1.0, (2, 2): 0.8}, outlined=_all_cells()
        ),
    )

    result = svc.extract_matrix_result(db, page, obj, batch)

    assert result.validity_forces_review is False
    assert result.confidence_score < 0.9
    assert result.review_required is True


# ---------------------------------------------------------------------------
# The row/column DEFINITIONS are labels, not the grid
# ---------------------------------------------------------------------------
def test_a_matrix_without_row_or_column_definitions_is_still_read(
    db, pages_dir, project, actor, template_page
):
    """``rows_count`` / ``cols_count`` decide what the export PRINTS —
    ``_render_matrix`` falls back to ``str(index)`` for a missing label — so a
    matrix with no definitions still has a full grid of bubbles on the sheet.
    Iterating the definitions instead of the counts would skip cells that
    physically exist, which is the one thing extraction must never do."""
    obj = _make_matrix(
        db, template_page,
        rule=MatrixSelectionRule.ONE_PER_ROW,
        with_definitions=False,
    )
    batch = _make_batch(db, project, actor, threshold=0.3)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan({(2, 3): 1.0}, outlined=_all_cells()),
    )

    result = svc.extract_matrix_result(db, page, obj, batch)

    assert len(_rows(result)) == ROWS
    assert [c["col_index"] for c in _rows(result)[2]["selected_cells"]] == [3]


# ---------------------------------------------------------------------------
# The degenerate matrix
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "rows, cols", [(0, 4), (3, 0), (0, 0), (-1, 4), (3, -2)]
)
def test_a_matrix_with_no_grid_reads_as_an_empty_payload_not_an_error(
    db, pages_dir, project, actor, template_page, rows, cols
):
    """The same defensive guard ``_render_matrix`` applies, on the other side of
    the same contract: it prints no grid, so there is nothing on the sheet to
    read. Not an exception — an object nobody could answer."""
    obj = _make_matrix(
        db, template_page,
        rule=MatrixSelectionRule.ONE_PER_ROW,
        rows=rows,
        cols=cols,
        with_definitions=False,
    )
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(db, pages_dir, batch, template_page, scan=_draw_scan())

    result = svc.extract_matrix_result(db, page, obj, batch)

    assert result.detected_payload_json == {"rows": [], "cols": []}
    assert result.confidence_score is None
    assert result.validity_forces_review is False
    assert result.review_required is False


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------
def test_a_respondent_identifier_matrix_is_refused_outright(
    db, pages_dir, project, actor, template_page
):
    """A respondent-ID grid is not an evaluable question: its cells spell a
    candidate's id and carry no Validity. The orchestrator's dispatch is
    what keeps it away from here, and this guard is what makes a dispatch bug
    raise instead of silently writing a wrong reading."""
    obj = _make_matrix(
        db, template_page,
        rule=MatrixSelectionRule.ONE_PER_ROW,
        role=PageObjectRole.RESPONDENT_IDENTIFIER,
    )
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page, scan=_draw_scan({(0, 0): 1.0})
    )

    with pytest.raises(ValueError, match="RESPONDENT_IDENTIFIER"):
        svc.extract_matrix_result(db, page, obj, batch)


def test_an_unmatched_page_raises_rather_than_guessing(
    db, pages_dir, project, actor, template_page
):
    """No ``form_variant_page_id`` means no template geometry to map from.
    Matching runs first; that is a precondition of the caller, not a
    case to be handled here by guessing."""
    obj = _make_matrix(db, template_page)
    batch = _make_batch(db, project, actor)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan({(0, 0): 1.0}), matched=False,
    )

    with pytest.raises(ValueError, match="not matched"):
        svc.extract_matrix_result(db, page, obj, batch)


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
        svc.extract_matrix_result(db, page, obj, batch)


# ---------------------------------------------------------------------------
# The staged row itself
# ---------------------------------------------------------------------------
def test_the_result_is_typed_matrix_and_staged_not_committed(
    db, pages_dir, project, actor, template_page
):
    """The caller owns the transaction, the same discipline as QUESTION
    extraction."""
    obj = _make_matrix(db, template_page)
    batch = _make_batch(db, project, actor, threshold=0.3)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan({(0, 0): 1.0}, outlined=_all_cells()),
    )

    result = svc.extract_matrix_result(db, page, obj, batch)

    assert result.result_type is SubmissionResultType.MATRIX
    assert result.page_object_id == obj.id
    assert result.submission_page_id == page.id
    assert result in db.new
    assert isinstance(result, SubmissionObjectResult)


def test_the_reading_survives_a_flush_as_plain_json(
    db, pages_dir, project, actor, template_page
):
    """The payload goes into a JSONB column: what comes back out has to be the
    same reading, through Pydantic's json mode and back."""
    obj = _make_matrix(db, template_page, rule=MatrixSelectionRule.ONE_PER_ROW)
    batch = _make_batch(db, project, actor, threshold=0.3)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan({(1, 2): 1.0}, outlined=_all_cells()),
    )

    result = svc.extract_matrix_result(db, page, obj, batch)
    db.flush()
    db.refresh(result)

    assert result.detected_payload_json["rows"][1] == {
        "row_index": 1,
        "selected_cells": [
            {"col_index": 2, "confidence": pytest.approx(result.confidence_score)}
        ],
        "validity": Validity.VALID.value,
    }


@pytest.mark.parametrize("scale", [1.0, 2.0, 6.0])
def test_the_same_sheet_reads_the_same_at_any_resolution(
    db, pages_dir, project, actor, template_page, scale
):
    """The mapping uses the observed dimensional ratio and nothing about the
    file's DPI history, so a matrix scanned at 96 or at 600 DPI reads alike."""
    obj = _make_matrix(db, template_page, rule=MatrixSelectionRule.ONE_PER_ROW)
    batch = _make_batch(db, project, actor, threshold=0.3)
    page = _make_scanned_page(
        db, pages_dir, batch, template_page,
        scan=_draw_scan({(1, 2): 1.0}, outlined=_all_cells(), scale=scale),
    )

    result = svc.extract_matrix_result(db, page, obj, batch)

    assert [
        (g["row_index"], c["col_index"])
        for g in _rows(result)
        for c in g["selected_cells"]
    ] == [(1, 2)]
