# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Data-access layer for MatrixObjectConfig and its row/col/cell defs."""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.matrix_cell_def import MatrixCellDef
from app.models.matrix_col_def import MatrixColDef
from app.models.matrix_object_config import MatrixObjectConfig
from app.models.matrix_row_def import MatrixRowDef


# --- config ---------------------------------------------------------------


def add_config(db: Session, config: MatrixObjectConfig) -> MatrixObjectConfig:
    db.add(config)
    db.flush()
    return config


# --- rows ------------------------------------------------------------------


def get_row(db: Session, row_id: uuid.UUID) -> MatrixRowDef | None:
    return db.get(MatrixRowDef, row_id)


def add_row(db: Session, row: MatrixRowDef) -> MatrixRowDef:
    db.add(row)
    db.flush()
    return row


def list_rows(db: Session, config_id: uuid.UUID) -> list[MatrixRowDef]:
    stmt = (
        select(MatrixRowDef)
        .where(MatrixRowDef.matrix_object_config_id == config_id)
        .order_by(MatrixRowDef.order_index)
    )
    return list(db.scalars(stmt).all())


def row_order_exists(
    db: Session, config_id: uuid.UUID, order_index: int,
    *, exclude_id: uuid.UUID | None = None,
) -> bool:
    stmt = select(MatrixRowDef.id).where(
        MatrixRowDef.matrix_object_config_id == config_id,
        MatrixRowDef.order_index == order_index,
    )
    if exclude_id is not None:
        stmt = stmt.where(MatrixRowDef.id != exclude_id)
    return db.scalars(stmt).first() is not None


def max_row_order_index(db: Session, config_id: uuid.UUID) -> int | None:
    stmt = select(MatrixRowDef.order_index).where(
        MatrixRowDef.matrix_object_config_id == config_id
    ).order_by(MatrixRowDef.order_index.desc())
    return db.scalars(stmt).first()


def delete_row(db: Session, row: MatrixRowDef) -> None:
    db.delete(row)
    db.flush()


# --- cols ------------------------------------------------------------------


def get_col(db: Session, col_id: uuid.UUID) -> MatrixColDef | None:
    return db.get(MatrixColDef, col_id)


def add_col(db: Session, col: MatrixColDef) -> MatrixColDef:
    db.add(col)
    db.flush()
    return col


def list_cols(db: Session, config_id: uuid.UUID) -> list[MatrixColDef]:
    stmt = (
        select(MatrixColDef)
        .where(MatrixColDef.matrix_object_config_id == config_id)
        .order_by(MatrixColDef.order_index)
    )
    return list(db.scalars(stmt).all())


def col_order_exists(
    db: Session, config_id: uuid.UUID, order_index: int,
    *, exclude_id: uuid.UUID | None = None,
) -> bool:
    stmt = select(MatrixColDef.id).where(
        MatrixColDef.matrix_object_config_id == config_id,
        MatrixColDef.order_index == order_index,
    )
    if exclude_id is not None:
        stmt = stmt.where(MatrixColDef.id != exclude_id)
    return db.scalars(stmt).first() is not None


def max_col_order_index(db: Session, config_id: uuid.UUID) -> int | None:
    stmt = select(MatrixColDef.order_index).where(
        MatrixColDef.matrix_object_config_id == config_id
    ).order_by(MatrixColDef.order_index.desc())
    return db.scalars(stmt).first()


def delete_col(db: Session, col: MatrixColDef) -> None:
    db.delete(col)
    db.flush()


# --- cells -----------------------------------------------------------------


def get_cell(db: Session, cell_id: uuid.UUID) -> MatrixCellDef | None:
    return db.get(MatrixCellDef, cell_id)


def add_cell(db: Session, cell: MatrixCellDef) -> MatrixCellDef:
    db.add(cell)
    db.flush()
    return cell


def list_cells(db: Session, config_id: uuid.UUID) -> list[MatrixCellDef]:
    stmt = (
        select(MatrixCellDef)
        .where(MatrixCellDef.matrix_object_config_id == config_id)
        .order_by(MatrixCellDef.row_index, MatrixCellDef.col_index)
    )
    return list(db.scalars(stmt).all())


def cell_coords_exist(
    db: Session, config_id: uuid.UUID, row_index: int, col_index: int,
    *, exclude_id: uuid.UUID | None = None,
) -> bool:
    stmt = select(MatrixCellDef.id).where(
        MatrixCellDef.matrix_object_config_id == config_id,
        MatrixCellDef.row_index == row_index,
        MatrixCellDef.col_index == col_index,
    )
    if exclude_id is not None:
        stmt = stmt.where(MatrixCellDef.id != exclude_id)
    return db.scalars(stmt).first() is not None


def max_row_index_in_use(db: Session, config_id: uuid.UUID) -> int | None:
    """Highest row index referenced by any row def OR cell def."""
    row_max = db.scalars(
        select(MatrixRowDef.order_index)
        .where(MatrixRowDef.matrix_object_config_id == config_id)
        .order_by(MatrixRowDef.order_index.desc())
    ).first()
    cell_max = db.scalars(
        select(MatrixCellDef.row_index)
        .where(MatrixCellDef.matrix_object_config_id == config_id)
        .order_by(MatrixCellDef.row_index.desc())
    ).first()
    candidates = [v for v in (row_max, cell_max) if v is not None]
    return max(candidates) if candidates else None


def max_col_index_in_use(db: Session, config_id: uuid.UUID) -> int | None:
    """Highest col index referenced by any col def OR cell def."""
    col_max = db.scalars(
        select(MatrixColDef.order_index)
        .where(MatrixColDef.matrix_object_config_id == config_id)
        .order_by(MatrixColDef.order_index.desc())
    ).first()
    cell_max = db.scalars(
        select(MatrixCellDef.col_index)
        .where(MatrixCellDef.matrix_object_config_id == config_id)
        .order_by(MatrixCellDef.col_index.desc())
    ).first()
    candidates = [v for v in (col_max, cell_max) if v is not None]
    return max(candidates) if candidates else None


def delete_cell(db: Session, cell: MatrixCellDef) -> None:
    db.delete(cell)
    db.flush()
