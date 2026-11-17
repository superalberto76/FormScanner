# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure selection-rule logic for MATRIX cells.

A MATRIX' ``selection_rule`` constrains how many cells may be marked
``is_correct`` within a row / column:

  - ``ONE_PER_ROW``     : at most one correct cell per row
  - ``ONE_PER_COLUMN``  : at most one correct cell per column
  - ``MANY_PER_ROW`` / ``MANY_PER_COLUMN`` / ``FREE`` : unconstrained

These helpers are deliberately free of any DB / ORM dependency so they can be
unit-tested in isolation and reused by every persistence path (bulk create,
per-cell create / update). Inputs are duck-typed: any object exposing
``row_index``, ``col_index`` and ``is_correct`` works (ORM rows and create DTOs
alike).
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Optional, Protocol

from app.models.enums import MatrixSelectionRule

_SINGLE_SELECTION = (
    MatrixSelectionRule.ONE_PER_ROW,
    MatrixSelectionRule.ONE_PER_COLUMN,
)


class CellLike(Protocol):
    """Anything carrying a cell's coordinates and correct flag."""

    row_index: int
    col_index: int
    is_correct: bool


# The rules whose constraint runs ACROSS a row, and for which the row is
# therefore the group that behaves like an independent mini-QUESTION (MATRIX
# extraction, review and scoring). The two remaining rules — ONE_PER_COLUMN,
# MANY_PER_COLUMN — are column-oriented. Listed positively rather than as "not
# the column ones" so a rule added to the enum tomorrow fails loudly into the
# column branch instead of being silently absorbed into the majority behaviour.
# Kept in this pure module, not in ``matrix_extraction_service``, so the pure
# scoring rules can read it.
_ROW_ORIENTED = {
    MatrixSelectionRule.ONE_PER_ROW,
    MatrixSelectionRule.MANY_PER_ROW,
    MatrixSelectionRule.FREE,
}


def is_row_oriented(rule: MatrixSelectionRule) -> bool:
    """Whether *rule* makes the ROW the group carrying a validity verdict."""
    return rule in _ROW_ORIENTED


def is_single_selection(rule: MatrixSelectionRule) -> bool:
    """True for rules that allow at most one correct cell per group."""
    return rule in _SINGLE_SELECTION


def group_label(rule: MatrixSelectionRule) -> str:
    """Human-readable group name for error messages ("row" / "column")."""
    return "row" if rule == MatrixSelectionRule.ONE_PER_ROW else "column"


def _group_key(rule: MatrixSelectionRule, cell: CellLike) -> int:
    return cell.row_index if rule == MatrixSelectionRule.ONE_PER_ROW else cell.col_index


def find_batch_conflict(
    rule: MatrixSelectionRule, cells: Iterable[CellLike]
) -> Optional[tuple[CellLike, CellLike]]:
    """Return the first pair of correct cells sharing a row/column, else ``None``.

    Validates a whole set of cells at once (matrix creation). Always ``None`` for
    ``MANY_PER_*`` / ``FREE``.
    """
    if not is_single_selection(rule):
        return None
    seen: dict[int, CellLike] = {}
    for cell in cells:
        if not cell.is_correct:
            continue
        key = _group_key(rule, cell)
        if key in seen:
            return seen[key], cell
        seen[key] = cell
    return None


def find_conflicting_sibling(
    rule: MatrixSelectionRule,
    *,
    row_index: int,
    col_index: int,
    is_correct: bool,
    existing: Iterable[CellLike],
    exclude_id: object = None,
) -> Optional[CellLike]:
    """Return an already-correct cell conflicting with the candidate, else ``None``.

    For the per-cell create / update path: the candidate is the cell being
    written (``row_index`` / ``col_index`` / ``is_correct``), ``existing`` the
    currently persisted cells. Returns ``None`` when the candidate is not
    correct, the rule is not single-selection, or no sibling in the same group is
    correct. ``exclude_id`` skips the candidate's own persisted row on update.
    """
    if not is_correct or not is_single_selection(rule):
        return None
    for other in existing:
        if exclude_id is not None and getattr(other, "id", None) == exclude_id:
            continue
        if not other.is_correct:
            continue
        if rule == MatrixSelectionRule.ONE_PER_ROW and other.row_index == row_index:
            return other
        if rule == MatrixSelectionRule.ONE_PER_COLUMN and other.col_index == col_index:
            return other
    return None
