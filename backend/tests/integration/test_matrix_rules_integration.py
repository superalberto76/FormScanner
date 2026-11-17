# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for selection_rule enforcement.

Exercises the real service wiring (auth, persistence, the 422 reject) end to end
against Postgres: matrix creation (batch validation), per-cell create and
per-cell update. Requires TEST_DATABASE_URL — see conftest.py.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.enums import MatrixSelectionRule as R
from app.models.enums import PageObjectType
from app.models.page_object import PageObject
from app.repositories import matrix_object_repo
from app.schemas.matrix_object import (
    MatrixCellDefCreate,
    MatrixCellDefUpdate,
    MatrixColDefCreate,
    MatrixObjectConfigCreate,
    MatrixPageObjectCreate,
    MatrixRowDefCreate,
)
from app.services import matrix_object_service as svc


def _matrix_data(rule, cells, *, rows=2, cols=2):
    return MatrixPageObjectCreate(
        x=60, y=60, width=100, height=100,
        config=MatrixObjectConfigCreate(
            rows_count=rows, cols_count=cols, evaluative=True, selection_rule=rule,
            rows=[MatrixRowDefCreate(order_index=i, label=f"R{i}") for i in range(rows)],
            cols=[MatrixColDefCreate(order_index=i, label=f"C{i}") for i in range(cols)],
            cells=cells,
        ),
    )


def _created_object(db, page) -> PageObject:
    obj = db.scalars(
        select(PageObject).where(PageObject.form_variant_page_id == page.id)
    ).one()
    assert obj.object_type == PageObjectType.MATRIX
    return obj


def _make_matrix(db, page, user, rule, cells) -> PageObject:
    svc.create_matrix(db, page.id, _matrix_data(rule, cells), user)
    return _created_object(db, page)


def _correct_count(db, obj) -> int:
    return sum(c.is_correct for c in matrix_object_repo.list_cells(db, obj.matrix_config.id))


# --------------------------------------------------------------------------
# Creation — batch validation
# --------------------------------------------------------------------------
def test_create_matrix_one_correct_per_row_ok(db, page, user):
    obj = _make_matrix(db, page, user, R.ONE_PER_ROW, [
        MatrixCellDefCreate(row_index=0, col_index=0, is_correct=True),
        MatrixCellDefCreate(row_index=1, col_index=1, is_correct=True),
    ])
    assert _correct_count(db, obj) == 2


def test_create_matrix_two_correct_same_row_rejected(db, page, user):
    data = _matrix_data(R.ONE_PER_ROW, [
        MatrixCellDefCreate(row_index=0, col_index=0, is_correct=True),
        MatrixCellDefCreate(row_index=0, col_index=1, is_correct=True),
    ])
    with pytest.raises(HTTPException) as exc:
        svc.create_matrix(db, page.id, data, user)
    assert exc.value.status_code == 422


def test_create_matrix_two_correct_same_column_rejected(db, page, user):
    data = _matrix_data(R.ONE_PER_COLUMN, [
        MatrixCellDefCreate(row_index=0, col_index=0, is_correct=True),
        MatrixCellDefCreate(row_index=1, col_index=0, is_correct=True),
    ])
    with pytest.raises(HTTPException) as exc:
        svc.create_matrix(db, page.id, data, user)
    assert exc.value.status_code == 422


def test_create_matrix_many_per_row_allows_multiple(db, page, user):
    obj = _make_matrix(db, page, user, R.MANY_PER_ROW, [
        MatrixCellDefCreate(row_index=0, col_index=0, is_correct=True),
        MatrixCellDefCreate(row_index=0, col_index=1, is_correct=True),
    ])
    assert _correct_count(db, obj) == 2


# --------------------------------------------------------------------------
# Per-cell create
# --------------------------------------------------------------------------
def test_create_cell_second_correct_same_row_rejected(db, page, user):
    obj = _make_matrix(db, page, user, R.ONE_PER_ROW, [
        MatrixCellDefCreate(row_index=0, col_index=0, is_correct=True),
    ])
    with pytest.raises(HTTPException) as exc:
        svc.create_cell(
            db, obj.id,
            MatrixCellDefCreate(row_index=0, col_index=1, is_correct=True), user,
        )
    assert exc.value.status_code == 422


def test_create_cell_correct_different_row_ok(db, page, user):
    obj = _make_matrix(db, page, user, R.ONE_PER_ROW, [
        MatrixCellDefCreate(row_index=0, col_index=0, is_correct=True),
    ])
    cell = svc.create_cell(
        db, obj.id,
        MatrixCellDefCreate(row_index=1, col_index=0, is_correct=True), user,
    )
    assert cell.is_correct is True


# --------------------------------------------------------------------------
# Per-cell update
# --------------------------------------------------------------------------
def _cell_at(db, obj, row, col):
    return next(
        c for c in matrix_object_repo.list_cells(db, obj.matrix_config.id)
        if c.row_index == row and c.col_index == col
    )


def test_update_cell_mark_second_correct_same_row_rejected(db, page, user):
    obj = _make_matrix(db, page, user, R.ONE_PER_ROW, [
        MatrixCellDefCreate(row_index=0, col_index=0, is_correct=True),
        MatrixCellDefCreate(row_index=0, col_index=1, is_correct=False),
    ])
    target = _cell_at(db, obj, 0, 1)
    with pytest.raises(HTTPException) as exc:
        svc.update_cell(db, target.id, MatrixCellDefUpdate(is_correct=True), user)
    assert exc.value.status_code == 422


def test_update_cell_rescore_existing_correct_ok(db, page, user):
    # Re-saving the already-correct cell (only its score) must NOT 422 (self-exclude).
    obj = _make_matrix(db, page, user, R.ONE_PER_ROW, [
        MatrixCellDefCreate(row_index=0, col_index=0, is_correct=True, score=1.0),
    ])
    target = _cell_at(db, obj, 0, 0)
    updated = svc.update_cell(db, target.id, MatrixCellDefUpdate(score=5.0), user)
    assert updated.is_correct is True
    assert updated.score == 5.0


def test_update_cell_move_correct_within_row_after_clearing_ok(db, page, user):
    # The supported "move" flow: clear the old correct, then mark the new one.
    obj = _make_matrix(db, page, user, R.ONE_PER_ROW, [
        MatrixCellDefCreate(row_index=0, col_index=0, is_correct=True),
        MatrixCellDefCreate(row_index=0, col_index=1, is_correct=False),
    ])
    old = _cell_at(db, obj, 0, 0)
    svc.update_cell(db, old.id, MatrixCellDefUpdate(is_correct=False), user)
    new = _cell_at(db, obj, 0, 1)
    updated = svc.update_cell(db, new.id, MatrixCellDefUpdate(is_correct=True), user)
    assert updated.is_correct is True
    assert _correct_count(db, obj) == 1
