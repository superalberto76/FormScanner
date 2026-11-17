# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the pure MATRIX selection-rule logic.

These cover the decision logic that constrains how many cells may be marked
``is_correct`` per row / column. They are intentionally DB-free — the functions
under test take plain cell-like objects — so the suite runs with just pytest
(no Postgres, no app wiring).
"""
from dataclasses import dataclass

import pytest

from app.models.enums import MatrixSelectionRule as R
from app.services import matrix_selection as ms


@dataclass
class Cell:
    """Minimal stand-in for a MatrixCellDef / create DTO."""

    row_index: int
    col_index: int
    is_correct: bool = False
    id: object = None


# --------------------------------------------------------------------------
# is_single_selection
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "rule,expected",
    [
        (R.ONE_PER_ROW, True),
        (R.ONE_PER_COLUMN, True),
        (R.MANY_PER_ROW, False),
        (R.MANY_PER_COLUMN, False),
        (R.FREE, False),
    ],
)
def test_is_single_selection(rule, expected):
    assert ms.is_single_selection(rule) is expected


def test_group_label():
    assert ms.group_label(R.ONE_PER_ROW) == "row"
    assert ms.group_label(R.ONE_PER_COLUMN) == "column"


# --------------------------------------------------------------------------
# find_batch_conflict — used on matrix creation
# --------------------------------------------------------------------------
def test_batch_one_correct_per_row_ok():
    cells = [Cell(0, 0, True), Cell(0, 1, False), Cell(1, 0, True)]
    assert ms.find_batch_conflict(R.ONE_PER_ROW, cells) is None


def test_batch_two_correct_same_row_conflicts():
    a, b = Cell(0, 0, True), Cell(0, 2, True)
    assert ms.find_batch_conflict(R.ONE_PER_ROW, [a, Cell(1, 0, False), b]) == (a, b)


def test_batch_two_correct_same_column_conflicts():
    a, b = Cell(0, 1, True), Cell(2, 1, True)
    assert ms.find_batch_conflict(R.ONE_PER_COLUMN, [a, b]) == (a, b)


def test_batch_same_row_two_correct_ok_under_one_per_column():
    # two correct cells in the same ROW are fine under ONE_PER_COLUMN
    cells = [Cell(0, 0, True), Cell(0, 1, True)]
    assert ms.find_batch_conflict(R.ONE_PER_COLUMN, cells) is None


@pytest.mark.parametrize("rule", [R.MANY_PER_ROW, R.MANY_PER_COLUMN, R.FREE])
def test_batch_unconstrained_rules_never_conflict(rule):
    cells = [Cell(0, 0, True), Cell(0, 1, True), Cell(0, 2, True)]
    assert ms.find_batch_conflict(rule, cells) is None


def test_batch_ignores_incorrect_cells():
    assert ms.find_batch_conflict(R.ONE_PER_ROW, [Cell(0, 0), Cell(0, 1)]) is None


# --------------------------------------------------------------------------
# find_conflicting_sibling — used on per-cell create / update
# --------------------------------------------------------------------------
def test_sibling_none_when_candidate_not_correct():
    existing = [Cell(0, 0, True, id="a")]
    assert ms.find_conflicting_sibling(
        R.ONE_PER_ROW, row_index=0, col_index=1, is_correct=False, existing=existing
    ) is None


def test_sibling_detected_same_row():
    existing = [Cell(0, 0, True, id="a"), Cell(1, 0, True, id="b")]
    sib = ms.find_conflicting_sibling(
        R.ONE_PER_ROW, row_index=0, col_index=2, is_correct=True, existing=existing
    )
    assert sib is not None and sib.id == "a"


def test_sibling_detected_same_column():
    existing = [Cell(0, 1, True, id="a")]
    sib = ms.find_conflicting_sibling(
        R.ONE_PER_COLUMN, row_index=3, col_index=1, is_correct=True, existing=existing
    )
    assert sib is not None and sib.id == "a"


def test_sibling_different_group_ok():
    existing = [Cell(0, 0, True, id="a")]
    assert ms.find_conflicting_sibling(
        R.ONE_PER_ROW, row_index=1, col_index=0, is_correct=True, existing=existing
    ) is None


def test_sibling_excludes_self_by_id_on_update():
    # re-saving the already-correct cell (e.g. only its score) must not conflict
    existing = [Cell(0, 0, True, id="self")]
    assert ms.find_conflicting_sibling(
        R.ONE_PER_ROW, row_index=0, col_index=0, is_correct=True,
        existing=existing, exclude_id="self",
    ) is None


@pytest.mark.parametrize("rule", [R.MANY_PER_ROW, R.MANY_PER_COLUMN, R.FREE])
def test_sibling_unconstrained_rules(rule):
    existing = [Cell(0, 0, True, id="a")]
    assert ms.find_conflicting_sibling(
        rule, row_index=0, col_index=1, is_correct=True, existing=existing
    ) is None
