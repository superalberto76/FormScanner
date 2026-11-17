# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Matrix row/col/cell item endpoints.

Three routers mounted at /matrix-rows, /matrix-cols, /matrix-cells. Collection
routes (list/create under an object) live in page_objects.py.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.deps import get_db, require_user
from app.models.user import User
from app.schemas.matrix_object import (
    MatrixCellDefRead,
    MatrixCellDefUpdate,
    MatrixColDefRead,
    MatrixColDefUpdate,
    MatrixRowDefRead,
    MatrixRowDefUpdate,
)
from app.services import matrix_object_service

rows_router = APIRouter()   # mounted at /matrix-rows
cols_router = APIRouter()   # mounted at /matrix-cols
cells_router = APIRouter()  # mounted at /matrix-cells


# --- rows ---


@rows_router.patch("/{row_id}", response_model=MatrixRowDefRead)
def update_matrix_row(
    row_id: uuid.UUID,
    data: MatrixRowDefUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    return matrix_object_service.update_row(db, row_id, data, user)


@rows_router.delete("/{row_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_matrix_row(
    row_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    matrix_object_service.delete_row(db, row_id, user)


# --- cols ---


@cols_router.patch("/{col_id}", response_model=MatrixColDefRead)
def update_matrix_col(
    col_id: uuid.UUID,
    data: MatrixColDefUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    return matrix_object_service.update_col(db, col_id, data, user)


@cols_router.delete("/{col_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_matrix_col(
    col_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    matrix_object_service.delete_col(db, col_id, user)


# --- cells ---


@cells_router.patch("/{cell_id}", response_model=MatrixCellDefRead)
def update_matrix_cell(
    cell_id: uuid.UUID,
    data: MatrixCellDefUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    return matrix_object_service.update_cell(db, cell_id, data, user)


@cells_router.delete("/{cell_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_matrix_cell(
    cell_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    matrix_object_service.delete_cell(db, cell_id, user)
