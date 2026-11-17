# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure (DB-free) tests for MATRIX extraction.

Everything here is arithmetic or enum logic: the grid geometry shared
between export and extraction, the per-group validity rule, and the
orientation split that decides whether a row or a column is the group. No
Postgres, no session, no image — the DB-backed half lives in
``tests/integration/test_matrix_extraction_integration.py``.

The geometry test is the one worth reading twice: it is a NON-REGRESSION test
of ``app.utils.matrix_layout``, which
``form_variant_export_service._render_matrix`` prints with. It writes the grid
arithmetic out literally and asserts the shared function agrees, so "the
printed grid has these coordinates" is a checked claim rather than a careful
reading of the code.
"""
from __future__ import annotations

import pytest

from app.models.enums import MatrixSelectionRule, Validity
from app.services import matrix_extraction_service as svc
from app.services import form_variant_export_service as export_svc
from app.utils import matrix_layout, question_layout


ROW_RULES = [
    MatrixSelectionRule.ONE_PER_ROW,
    MatrixSelectionRule.MANY_PER_ROW,
    MatrixSelectionRule.FREE,
]
COLUMN_RULES = [
    MatrixSelectionRule.ONE_PER_COLUMN,
    MatrixSelectionRule.MANY_PER_COLUMN,
]


# ---------------------------------------------------------------------------
# The shared layout constants really are shared
# ---------------------------------------------------------------------------
def test_export_and_extraction_read_the_same_matrix_constants():
    """The MATRIX half of the point of ``app.utils.matrix_layout`` — the same
    property ``test_question_extraction`` asserts for QUESTION. Identity, not
    equality: two modules holding equal-for-now copies is exactly the state
    the shared module exists to prevent."""
    assert export_svc._M_LABEL_H is matrix_layout.LABEL_H
    assert export_svc._M_HEADER_H is matrix_layout.HEADER_H
    # The cell bubble IS the QUESTION option circle, printed by the same call.
    # A MATRIX-specific radius would be a second number saying the same thing.
    assert export_svc._Q_CIRCLE_R is question_layout.CIRCLE_R


# ---------------------------------------------------------------------------
# compute_grid_geometry — non-regression against the literal arithmetic
# ---------------------------------------------------------------------------
def _reference_geometry(obj_x, obj_y, obj_width, obj_height, rows, cols):
    """The grid arithmetic of ``_render_matrix``, written out literally.

    Spelled out here on purpose: a helper that called the function under test
    would prove nothing.
    """
    label_w = min(obj_width * 0.25, 120.0)
    top = obj_y + 16          # _M_LABEL_H
    grid_x = obj_x + label_w
    grid_y = top + 16         # _M_HEADER_H
    grid_w = max(obj_width - label_w, 1.0)
    grid_h = max((obj_y + obj_height) - grid_y, 1.0)
    return label_w, grid_x, grid_y, grid_w / cols, grid_h / rows


@pytest.mark.parametrize(
    "obj_x, obj_y, obj_width, obj_height, rows, cols",
    [
        (100.0, 200.0, 300.0, 200.0, 4, 5),      # ordinary
        (0.0, 0.0, 800.0, 400.0, 3, 3),          # label column hits the 120px cap
        (12.5, 37.25, 213.75, 91.5, 7, 2),       # non-integer geometry
        (50.0, 50.0, 60.0, 40.0, 1, 1),          # smaller than its own chrome
        (10.0, 10.0, 40.0, 20.0, 6, 6),          # grid_w / grid_h floored at 1.0
    ],
)
def test_the_grid_geometry_is_the_export_arithmetic_unchanged(
    obj_x, obj_y, obj_width, obj_height, rows, cols
):
    """Bit-for-bit, not approximately: if a coordinate moved by a fraction of a
    pixel, extraction would sample beside the printed bubble on a real scan."""
    geometry = matrix_layout.compute_grid_geometry(
        obj_x, obj_y, obj_width, obj_height, rows, cols
    )
    assert (
        geometry.label_w,
        geometry.grid_x,
        geometry.grid_y,
        geometry.cell_w,
        geometry.cell_h,
    ) == _reference_geometry(obj_x, obj_y, obj_width, obj_height, rows, cols)


def test_cell_center_is_the_middle_of_its_cell():
    geometry = matrix_layout.compute_grid_geometry(
        100.0, 200.0, 300.0, 260.0, rows=2, cols=4
    )
    cx, cy = matrix_layout.cell_center(geometry, row=1, col=2)

    assert cx == geometry.grid_x + 2 * geometry.cell_w + geometry.cell_w / 2
    assert cy == geometry.grid_y + 1 * geometry.cell_h + geometry.cell_h / 2


def test_cell_centres_are_the_ones_the_export_prints():
    """The export computes each circle's centre the same way; this is the
    coordinate an extractor has to land on."""
    obj_x, obj_y, obj_w, obj_h, rows, cols = 40.0, 60.0, 320.0, 240.0, 3, 4
    label_w, grid_x, grid_y, cell_w, cell_h = _reference_geometry(
        obj_x, obj_y, obj_w, obj_h, rows, cols
    )
    geometry = matrix_layout.compute_grid_geometry(
        obj_x, obj_y, obj_w, obj_h, rows, cols
    )

    for rr in range(rows):
        for c in range(cols):
            assert matrix_layout.cell_center(geometry, rr, c) == (
                grid_x + c * cell_w + cell_w / 2,
                grid_y + rr * cell_h + cell_h / 2,
            )


def test_the_label_column_never_takes_more_than_120px():
    geometry = matrix_layout.compute_grid_geometry(
        0.0, 0.0, 2000.0, 300.0, rows=2, cols=2
    )
    assert geometry.label_w == 120.0


def test_a_matrix_smaller_than_its_own_chrome_still_has_positive_cells():
    """The floor at 1.0px, stated: an object authored shorter than its label and
    header bands must produce a squashed grid, not a negative one."""
    geometry = matrix_layout.compute_grid_geometry(
        0.0, 0.0, 30.0, 10.0, rows=3, cols=3
    )
    assert geometry.cell_w > 0
    assert geometry.cell_h > 0


# ---------------------------------------------------------------------------
# The export draws exactly where the geometry says
# ---------------------------------------------------------------------------
class _RecordingRenderer:
    """A PageRenderer stand-in that records the calls instead of drawing them.

    ``_render_matrix`` takes a renderer and nothing else that needs a PDF, so
    this is enough to check WHERE it puts things — the only thing extraction
    depends on.
    """

    def __init__(self) -> None:
        self.circles: list[tuple[float, float, float]] = []
        self.boxes: list[tuple[float, float, float, float]] = []
        self.texts: list[tuple[float, float, float, float, str]] = []

    def text(self, x, y, w, h, text, px_size, **kwargs) -> None:
        self.texts.append((x, y, w, h, text))

    def box(self, x, y, w, h, **kwargs) -> None:
        self.boxes.append((x, y, w, h))

    def circle(self, cx, cy, r, **kwargs) -> None:
        self.circles.append((cx, cy, r))


class _FakeMatrixObject:
    """The handful of attributes ``_render_matrix`` reads off a PageObject."""

    def __init__(self, x, y, width, height, rows, cols):
        self.x, self.y = x, y
        self.width, self.height = width, height
        self.label = "Matrix"
        self.matrix_config = type(
            "Cfg", (), {"rows_count": rows, "cols_count": cols,
                        "rows": [], "cols": [], "cells": []}
        )()


def test_the_export_draws_its_cells_where_it_always_did():
    """Behavioural non-regression: the circles come out at the coordinates the
    literal arithmetic computes, in row-major order, at the option radius."""
    obj = _FakeMatrixObject(60.0, 120.0, 400.0, 300.0, rows=3, cols=4)
    renderer = _RecordingRenderer()

    export_svc._render_matrix(renderer, obj, export_svc.ExportMode.STUDENT)

    label_w, grid_x, grid_y, cell_w, cell_h = _reference_geometry(
        obj.x, obj.y, obj.width, obj.height, 3, 4
    )
    assert renderer.circles == [
        (
            grid_x + c * cell_w + cell_w / 2,
            grid_y + rr * cell_h + cell_h / 2,
            question_layout.CIRCLE_R,
        )
        for rr in range(3)
        for c in range(4)
    ]
    # …and the cell frames and the row-label column with them.
    assert renderer.boxes == [
        (grid_x + c * cell_w, grid_y + rr * cell_h, cell_w, cell_h)
        for rr in range(3)
        for c in range(4)
    ]
    assert (obj.x, grid_y, label_w, cell_h) == renderer.texts[5][:4]


def test_the_export_draws_no_grid_for_a_degenerate_matrix():
    """The guard the extractor mirrors: no rows or no columns means nothing is
    printed, so there is nothing on the sheet to read either."""
    renderer = _RecordingRenderer()

    export_svc._render_matrix(
        renderer, _FakeMatrixObject(0.0, 0.0, 200.0, 200.0, rows=0, cols=4),
        export_svc.ExportMode.STUDENT,
    )

    assert renderer.circles == []
    assert renderer.boxes == []


# ---------------------------------------------------------------------------
# Orientation — which axis is the mini-QUESTION
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("rule", ROW_RULES)
def test_row_rules_make_the_row_the_group(rule):
    assert svc.is_row_oriented(rule) is True


@pytest.mark.parametrize("rule", COLUMN_RULES)
def test_column_rules_make_the_column_the_group(rule):
    """Not an exotic corner: ONE_PER_COLUMN is how "one item per category,
    exactly one per column" is authored, and its constraint — hence its
    validity verdict — runs down the columns."""
    assert svc.is_row_oriented(rule) is False


def test_every_selection_rule_has_an_orientation():
    """No rule may fall through unclassified: one that did would be read with
    the wrong group and produce a validity nobody authored."""
    for rule in MatrixSelectionRule:
        assert isinstance(svc.is_row_oriented(rule), bool)
    assert set(ROW_RULES) | set(COLUMN_RULES) == set(MatrixSelectionRule)


# ---------------------------------------------------------------------------
# _compute_group_validity — the discrete axis, per group
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("rule", list(MatrixSelectionRule))
def test_nothing_blackened_in_a_group_is_no_answer_under_every_rule(rule):
    """FREE included, and that is the deliberate reading: "no constraint" caps
    how MANY cells may be marked, it does not turn an untouched group into an
    answer."""
    assert svc._compute_group_validity(rule, 0) is Validity.NO_ANSWER


@pytest.mark.parametrize("rule", list(MatrixSelectionRule))
def test_one_selection_is_valid_under_every_rule(rule):
    assert svc._compute_group_validity(rule, 1) is Validity.VALID


@pytest.mark.parametrize(
    "rule", [MatrixSelectionRule.ONE_PER_ROW, MatrixSelectionRule.ONE_PER_COLUMN]
)
@pytest.mark.parametrize("n_selected", [2, 5])
def test_more_than_one_breaks_a_single_selection_rule(rule, n_selected):
    assert (
        svc._compute_group_validity(rule, n_selected)
        is Validity.TOO_MANY_ANSWERS
    )


@pytest.mark.parametrize(
    "rule",
    [
        MatrixSelectionRule.MANY_PER_ROW,
        MatrixSelectionRule.MANY_PER_COLUMN,
        MatrixSelectionRule.FREE,
    ],
)
@pytest.mark.parametrize("n_selected", [2, 9])
def test_more_than_one_is_the_point_of_a_multi_selection_rule(rule, n_selected):
    """FREE behaves as MANY_PER_ROW here: no upper limit at all."""
    assert svc._compute_group_validity(rule, n_selected) is Validity.VALID


def test_the_single_selection_verdict_is_the_authoring_one():
    """Asked of ``matrix_selection.is_single_selection`` rather than
    re-derived: two answers to "does this rule allow one cell per group" would
    eventually disagree, and a sheet the authoring tool called invalid would be
    read as valid."""
    from app.services import matrix_selection

    for rule in MatrixSelectionRule:
        too_many = svc._compute_group_validity(rule, 2) is Validity.TOO_MANY_ANSWERS
        assert too_many is matrix_selection.is_single_selection(rule)


# ---------------------------------------------------------------------------
# Grouping the readings — rows and columns are mirror images
# ---------------------------------------------------------------------------
def test_rows_are_built_from_the_readings_of_their_own_cells():
    selected = {(0, 1): 0.9, (0, 2): 0.8, (2, 0): 0.7}

    rows = svc._build_rows(
        selected, MatrixSelectionRule.MANY_PER_ROW, rows_count=3, cols_count=3
    )

    assert [r.row_index for r in rows] == [0, 1, 2]
    assert [c.col_index for c in rows[0].selected_cells] == [1, 2]
    assert rows[1].selected_cells == []
    assert [c.col_index for c in rows[2].selected_cells] == [0]
    assert [r.validity for r in rows] == [
        Validity.VALID, Validity.NO_ANSWER, Validity.VALID
    ]


def test_columns_are_built_from_the_very_same_readings_the_other_way_round():
    """The symmetry, on one set of readings: the same three inked cells grouped
    by column instead of by row, referenced by ``row_index``."""
    selected = {(0, 1): 0.9, (0, 2): 0.8, (2, 0): 0.7}

    cols = svc._build_cols(
        selected, MatrixSelectionRule.MANY_PER_COLUMN, rows_count=3, cols_count=3
    )

    assert [c.col_index for c in cols] == [0, 1, 2]
    assert [c.row_index for c in cols[0].selected_cells] == [2]
    assert [c.row_index for c in cols[1].selected_cells] == [0]
    assert [c.row_index for c in cols[2].selected_cells] == [0]


def test_an_unmarked_group_is_reported_rather_than_dropped():
    """A skipped row and a row that was never printed are different facts, and
    only reporting every printed group keeps them apart."""
    rows = svc._build_rows(
        {}, MatrixSelectionRule.ONE_PER_ROW, rows_count=4, cols_count=2
    )

    assert len(rows) == 4
    assert all(r.validity is Validity.NO_ANSWER for r in rows)
    assert all(r.selected_cells == [] for r in rows)


def test_a_group_with_two_marks_under_a_single_selection_rule_is_too_many():
    rows = svc._build_rows(
        {(0, 0): 0.9, (0, 3): 0.85},
        MatrixSelectionRule.ONE_PER_ROW,
        rows_count=1,
        cols_count=4,
    )

    assert rows[0].validity is Validity.TOO_MANY_ANSWERS
    assert [c.col_index for c in rows[0].selected_cells] == [0, 3]


def test_the_fill_ratio_is_carried_through_as_the_cell_confidence():
    rows = svc._build_rows(
        {(0, 1): 0.83}, MatrixSelectionRule.FREE, rows_count=1, cols_count=2
    )
    cols = svc._build_cols(
        {(0, 1): 0.83}, MatrixSelectionRule.ONE_PER_COLUMN, rows_count=1, cols_count=2
    )

    assert rows[0].selected_cells[0].confidence == pytest.approx(0.83)
    assert cols[1].selected_cells[0].confidence == pytest.approx(0.83)
