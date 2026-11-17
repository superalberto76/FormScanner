# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""MATRIX PageObject service.

Covers MatrixObjectConfig plus MatrixRowDef / MatrixColDef / MatrixCellDef.

Conservative consistency rules:
  - row/col order_index is unique within the matrix and must be in
    [0, rows_count) / [0, cols_count)
  - cell (row_index, col_index) is unique and within bounds
  - shrinking rows_count/cols_count is rejected (409) while any row/col/cell
    still references an out-of-bounds index
  - on a respondent-ID grid (RESPONDENT_IDENTIFIER, a linked copy included) a
    row label cannot be blank or contain "?" (409); the same labels stay free
    on any other matrix
"""
from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.enums import PageObjectRole, PageObjectType
from app.models.matrix_cell_def import MatrixCellDef
from app.models.matrix_col_def import MatrixColDef
from app.models.matrix_object_config import MatrixObjectConfig
from app.models.matrix_row_def import MatrixRowDef
from app.models.page_object import PageObject
from app.models.user import User
from app.repositories import matrix_object_repo, page_object_repo
from app.schemas.matrix_object import (
    MatrixCellDefCreate,
    MatrixCellDefRead,
    MatrixCellDefUpdate,
    MatrixColDefCreate,
    MatrixColDefRead,
    MatrixColDefUpdate,
    MatrixObjectConfigUpdate,
    MatrixPageObjectCreate,
    MatrixRowDefCreate,
    MatrixRowDefRead,
    MatrixRowDefUpdate,
)
from app.services import audit_service, matrix_selection, page_object_service


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def _correct_conflict_error(rule, sibling: MatrixCellDef) -> HTTPException:
    """422 raised when marking a cell correct would break a single-selection rule."""
    return _unprocessable(
        f"selection_rule {rule.value} allows at most one correct cell per "
        f"{matrix_selection.group_label(rule)}; cell "
        f"({sibling.row_index},{sibling.col_index}) is already marked correct."
    )


# ---------------------------------------------------------------------------
# Create MATRIX object
# ---------------------------------------------------------------------------


def create_matrix(
    db: Session, page_id: uuid.UUID, data: MatrixPageObjectCreate, user: User
):
    page, _ = page_object_service.load_page_authorized(
        db, page_id, user, require_draft=True
    )
    # Reserved bands: nothing is placed in them.
    page_object_service.assert_outside_bands(data, page)
    cfg = data.config

    # Validate provided children against declared rows_count/cols_count.
    row_idx = [r.order_index for r in cfg.rows]
    col_idx = [c.order_index for c in cfg.cols]
    if len(row_idx) != len(set(row_idx)):
        raise _conflict("Duplicate row order_index in request.")
    if len(col_idx) != len(set(col_idx)):
        raise _conflict("Duplicate col order_index in request.")
    if any(i >= cfg.rows_count or i < 0 for i in row_idx):
        raise _conflict("A row order_index is out of bounds for rows_count.")
    if any(i >= cfg.cols_count or i < 0 for i in col_idx):
        raise _conflict("A col order_index is out of bounds for cols_count.")
    cell_coords = [(c.row_index, c.col_index) for c in cfg.cells]
    if len(cell_coords) != len(set(cell_coords)):
        raise _conflict("Duplicate cell (row_index, col_index) in request.")
    if any(
        rc[0] >= cfg.rows_count or rc[1] >= cfg.cols_count for rc in cell_coords
    ):
        raise _conflict("A cell coordinate is out of bounds for the matrix size.")
    # selection_rule: reject a batch that marks more than one cell
    # correct in the same row / column under ONE_PER_ROW / ONE_PER_COLUMN.
    batch_conflict = matrix_selection.find_batch_conflict(cfg.selection_rule, cfg.cells)
    if batch_conflict is not None:
        first, second = batch_conflict
        raise _unprocessable(
            f"selection_rule {cfg.selection_rule.value} allows at most one correct "
            f"cell per {matrix_selection.group_label(cfg.selection_rule)}; cells "
            f"({first.row_index},{first.col_index}) and "
            f"({second.row_index},{second.col_index}) are both marked correct."
        )

    obj = PageObject(
        form_variant_page_id=page.id,
        object_type=PageObjectType.MATRIX,
        x=data.x, y=data.y, width=data.width, height=data.height,
        z_index=data.z_index, label=data.label, description=data.description,
        visible=data.visible, locked=data.locked,
        # "Show on all pages": a fresh linked group, this object its
        # template. Existing pages aren't copied to; new pages get a linked copy.
        shared_group_id=uuid.uuid4() if data.shared_across_pages else None,
        shared_across_pages=data.shared_across_pages,
    )
    page_object_repo.add(db, obj)

    config = matrix_object_repo.add_config(
        db,
        MatrixObjectConfig(
            page_object_id=obj.id,
            rows_count=cfg.rows_count,
            cols_count=cfg.cols_count,
            evaluative=cfg.evaluative,
            selection_rule=cfg.selection_rule,
        ),
    )
    for r in cfg.rows:
        matrix_object_repo.add_row(
            db, MatrixRowDef(
                matrix_object_config_id=config.id,
                order_index=r.order_index, label=r.label,
            ),
        )
    for c in cfg.cols:
        matrix_object_repo.add_col(
            db, MatrixColDef(
                matrix_object_config_id=config.id,
                order_index=c.order_index, label=c.label,
            ),
        )
    for cell in cfg.cells:
        matrix_object_repo.add_cell(
            db, MatrixCellDef(
                matrix_object_config_id=config.id,
                row_index=cell.row_index, col_index=cell.col_index,
                is_correct=cell.is_correct, score=cell.score,
            ),
        )

    audit_service.log_event(
        db, actor_user_id=user.id, action=audit_service.PAGE_OBJECT_CREATED,
        entity_type="PageObject", entity_id=obj.id,
        metadata={
            "object_type": "MATRIX",
            "rows": len(cfg.rows), "cols": len(cfg.cols), "cells": len(cfg.cells),
        },
    )
    db.commit()
    db.refresh(obj)
    return page_object_service.build_typed_read(obj)


# ---------------------------------------------------------------------------
# Update MATRIX config (with conservative shrink guard)
# ---------------------------------------------------------------------------


def update_matrix_config(
    db: Session, object_id: uuid.UUID, data: MatrixObjectConfigUpdate, user: User
):
    config = _load_config_for_object(db, object_id, user, require_draft=True)

    changes = data.model_dump(exclude_unset=True)
    if not changes:
        return page_object_service.build_typed_read(config.page_object)

    new_rows = changes.get("rows_count")
    if new_rows is not None and new_rows < config.rows_count:
        max_row = matrix_object_repo.max_row_index_in_use(db, config.id)
        if max_row is not None and max_row >= new_rows:
            raise _conflict(
                f"Cannot shrink rows_count to {new_rows}: a row/cell uses index {max_row}."
            )
    new_cols = changes.get("cols_count")
    if new_cols is not None and new_cols < config.cols_count:
        max_col = matrix_object_repo.max_col_index_in_use(db, config.id)
        if max_col is not None and max_col >= new_cols:
            raise _conflict(
                f"Cannot shrink cols_count to {new_cols}: a col/cell uses index {max_col}."
            )

    for field, value in changes.items():
        setattr(config, field, value)
    db.flush()
    audit_service.log_event(
        db, actor_user_id=user.id, action=audit_service.PAGE_OBJECT_CONFIG_UPDATED,
        entity_type="MatrixObjectConfig", entity_id=config.id,
        metadata={k: (v.value if hasattr(v, "value") else v) for k, v in changes.items()},
    )
    db.commit()
    db.refresh(config.page_object)
    return page_object_service.build_typed_read(config.page_object)


# ---------------------------------------------------------------------------
# Shared loaders
# ---------------------------------------------------------------------------


def _load_config_for_object(
    db: Session, object_id: uuid.UUID, user: User, *, require_draft: bool
) -> MatrixObjectConfig:
    obj, _ = page_object_service.load_object_authorized(
        db, object_id, user, require_draft=require_draft
    )
    page_object_service.assert_object_type(obj, PageObjectType.MATRIX)
    if obj.matrix_config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Matrix config not found"
        )
    return obj.matrix_config


def _config_of(db: Session, config_id: uuid.UUID) -> MatrixObjectConfig:
    return db.get(MatrixObjectConfig, config_id)


def _authorize_via_config(
    db: Session, config: MatrixObjectConfig, user: User, *, require_draft: bool
) -> None:
    page_object_service.load_object_authorized(
        db, config.page_object_id, user, require_draft=require_draft
    )


def _check_identifier_row_label(
    config: MatrixObjectConfig, order_index: int, label: str
) -> None:
    """409 when *label* cannot name a row of this matrix: only a respondent-ID
    grid restricts its labels, through the role its object carries — its own
    or, for a linked copy, the group's (``effective_role``)."""
    if (
        config.page_object.effective_role is PageObjectRole.RESPONDENT_IDENTIFIER
        and page_object_service.invalid_identifier_row_label(label)
    ):
        raise page_object_service.identifier_row_label_conflict(order_index)


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


def list_rows(db: Session, object_id: uuid.UUID, user: User) -> list[MatrixRowDefRead]:
    config = _load_config_for_object(db, object_id, user, require_draft=False)
    return [MatrixRowDefRead.model_validate(r) for r in matrix_object_repo.list_rows(db, config.id)]


def create_row(
    db: Session, object_id: uuid.UUID, data: MatrixRowDefCreate, user: User
) -> MatrixRowDefRead:
    config = _load_config_for_object(db, object_id, user, require_draft=True)
    if data.order_index >= config.rows_count:
        raise _conflict(
            f"order_index {data.order_index} out of bounds for rows_count {config.rows_count}."
        )
    if matrix_object_repo.row_order_exists(db, config.id, data.order_index):
        raise _conflict(f"Row order_index {data.order_index} already exists.")
    _check_identifier_row_label(config, data.order_index, data.label)

    row = matrix_object_repo.add_row(
        db, MatrixRowDef(
            matrix_object_config_id=config.id,
            order_index=data.order_index, label=data.label,
        ),
    )
    audit_service.log_event(
        db, actor_user_id=user.id, action=audit_service.MATRIX_ROW_CREATED,
        entity_type="MatrixRowDef", entity_id=row.id,
    )
    db.commit()
    db.refresh(row)
    return MatrixRowDefRead.model_validate(row)


def update_row(
    db: Session, row_id: uuid.UUID, data: MatrixRowDefUpdate, user: User
) -> MatrixRowDefRead:
    row = matrix_object_repo.get_row(db, row_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matrix row not found")
    config = _config_of(db, row.matrix_object_config_id)
    _authorize_via_config(db, config, user, require_draft=True)

    changes = data.model_dump(exclude_unset=True)
    if changes:
        new_index = changes.get("order_index")
        if new_index is not None:
            if new_index >= config.rows_count:
                raise _conflict(
                    f"order_index {new_index} out of bounds for rows_count {config.rows_count}."
                )
            if matrix_object_repo.row_order_exists(
                db, config.id, new_index, exclude_id=row.id
            ):
                raise _conflict(f"Row order_index {new_index} already exists.")
        if changes.get("label") is not None:
            _check_identifier_row_label(
                config, changes.get("order_index", row.order_index), changes["label"]
            )
        for field, value in changes.items():
            setattr(row, field, value)
        db.flush()
        audit_service.log_event(
            db, actor_user_id=user.id, action=audit_service.MATRIX_ROW_UPDATED,
            entity_type="MatrixRowDef", entity_id=row.id, metadata=changes,
        )
        db.commit()
    db.refresh(row)
    return MatrixRowDefRead.model_validate(row)


def delete_row(db: Session, row_id: uuid.UUID, user: User) -> None:
    row = matrix_object_repo.get_row(db, row_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matrix row not found")
    config = _config_of(db, row.matrix_object_config_id)
    _authorize_via_config(db, config, user, require_draft=True)
    row_id_val = row.id
    matrix_object_repo.delete_row(db, row)
    audit_service.log_event(
        db, actor_user_id=user.id, action=audit_service.MATRIX_ROW_DELETED,
        entity_type="MatrixRowDef", entity_id=row_id_val,
    )
    db.commit()


# ---------------------------------------------------------------------------
# Cols
# ---------------------------------------------------------------------------


def list_cols(db: Session, object_id: uuid.UUID, user: User) -> list[MatrixColDefRead]:
    config = _load_config_for_object(db, object_id, user, require_draft=False)
    return [MatrixColDefRead.model_validate(c) for c in matrix_object_repo.list_cols(db, config.id)]


def create_col(
    db: Session, object_id: uuid.UUID, data: MatrixColDefCreate, user: User
) -> MatrixColDefRead:
    config = _load_config_for_object(db, object_id, user, require_draft=True)
    if data.order_index >= config.cols_count:
        raise _conflict(
            f"order_index {data.order_index} out of bounds for cols_count {config.cols_count}."
        )
    if matrix_object_repo.col_order_exists(db, config.id, data.order_index):
        raise _conflict(f"Col order_index {data.order_index} already exists.")

    col = matrix_object_repo.add_col(
        db, MatrixColDef(
            matrix_object_config_id=config.id,
            order_index=data.order_index, label=data.label,
        ),
    )
    audit_service.log_event(
        db, actor_user_id=user.id, action=audit_service.MATRIX_COL_CREATED,
        entity_type="MatrixColDef", entity_id=col.id,
    )
    db.commit()
    db.refresh(col)
    return MatrixColDefRead.model_validate(col)


def update_col(
    db: Session, col_id: uuid.UUID, data: MatrixColDefUpdate, user: User
) -> MatrixColDefRead:
    col = matrix_object_repo.get_col(db, col_id)
    if col is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matrix col not found")
    config = _config_of(db, col.matrix_object_config_id)
    _authorize_via_config(db, config, user, require_draft=True)

    changes = data.model_dump(exclude_unset=True)
    if changes:
        new_index = changes.get("order_index")
        if new_index is not None:
            if new_index >= config.cols_count:
                raise _conflict(
                    f"order_index {new_index} out of bounds for cols_count {config.cols_count}."
                )
            if matrix_object_repo.col_order_exists(
                db, config.id, new_index, exclude_id=col.id
            ):
                raise _conflict(f"Col order_index {new_index} already exists.")
        for field, value in changes.items():
            setattr(col, field, value)
        db.flush()
        audit_service.log_event(
            db, actor_user_id=user.id, action=audit_service.MATRIX_COL_UPDATED,
            entity_type="MatrixColDef", entity_id=col.id, metadata=changes,
        )
        db.commit()
    db.refresh(col)
    return MatrixColDefRead.model_validate(col)


def delete_col(db: Session, col_id: uuid.UUID, user: User) -> None:
    col = matrix_object_repo.get_col(db, col_id)
    if col is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matrix col not found")
    config = _config_of(db, col.matrix_object_config_id)
    _authorize_via_config(db, config, user, require_draft=True)
    col_id_val = col.id
    matrix_object_repo.delete_col(db, col)
    audit_service.log_event(
        db, actor_user_id=user.id, action=audit_service.MATRIX_COL_DELETED,
        entity_type="MatrixColDef", entity_id=col_id_val,
    )
    db.commit()


# ---------------------------------------------------------------------------
# Cells
# ---------------------------------------------------------------------------


def list_cells(db: Session, object_id: uuid.UUID, user: User) -> list[MatrixCellDefRead]:
    config = _load_config_for_object(db, object_id, user, require_draft=False)
    return [MatrixCellDefRead.model_validate(c) for c in matrix_object_repo.list_cells(db, config.id)]


def create_cell(
    db: Session, object_id: uuid.UUID, data: MatrixCellDefCreate, user: User
) -> MatrixCellDefRead:
    config = _load_config_for_object(db, object_id, user, require_draft=True)
    if data.row_index >= config.rows_count or data.col_index >= config.cols_count:
        raise _conflict(
            f"Cell ({data.row_index},{data.col_index}) out of bounds for "
            f"{config.rows_count}x{config.cols_count} matrix."
        )
    if matrix_object_repo.cell_coords_exist(db, config.id, data.row_index, data.col_index):
        raise _conflict(
            f"Cell ({data.row_index},{data.col_index}) already exists."
        )
    # selection_rule: refuse to create a second correct cell in the
    # same row / column under ONE_PER_ROW / ONE_PER_COLUMN.
    sibling = matrix_selection.find_conflicting_sibling(
        config.selection_rule,
        row_index=data.row_index, col_index=data.col_index, is_correct=data.is_correct,
        existing=matrix_object_repo.list_cells(db, config.id),
    )
    if sibling is not None:
        raise _correct_conflict_error(config.selection_rule, sibling)

    cell = matrix_object_repo.add_cell(
        db, MatrixCellDef(
            matrix_object_config_id=config.id,
            row_index=data.row_index, col_index=data.col_index,
            is_correct=data.is_correct, score=data.score,
        ),
    )
    audit_service.log_event(
        db, actor_user_id=user.id, action=audit_service.MATRIX_CELL_CREATED,
        entity_type="MatrixCellDef", entity_id=cell.id,
    )
    db.commit()
    db.refresh(cell)
    return MatrixCellDefRead.model_validate(cell)


def update_cell(
    db: Session, cell_id: uuid.UUID, data: MatrixCellDefUpdate, user: User
) -> MatrixCellDefRead:
    cell = matrix_object_repo.get_cell(db, cell_id)
    if cell is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matrix cell not found")
    config = _config_of(db, cell.matrix_object_config_id)
    _authorize_via_config(db, config, user, require_draft=True)

    changes = data.model_dump(exclude_unset=True)
    if changes:
        new_row = changes.get("row_index", cell.row_index)
        new_col = changes.get("col_index", cell.col_index)
        if new_row >= config.rows_count or new_col >= config.cols_count:
            raise _conflict(
                f"Cell ({new_row},{new_col}) out of bounds for "
                f"{config.rows_count}x{config.cols_count} matrix."
            )
        if ("row_index" in changes or "col_index" in changes) and (
            matrix_object_repo.cell_coords_exist(
                db, config.id, new_row, new_col, exclude_id=cell.id
            )
        ):
            raise _conflict(f"Cell ({new_row},{new_col}) already exists.")
        # selection_rule: reject if this update would mark the cell
        # correct while another cell in the same row / column already is.
        new_correct = changes.get("is_correct", cell.is_correct)
        sibling = matrix_selection.find_conflicting_sibling(
            config.selection_rule,
            row_index=new_row, col_index=new_col, is_correct=new_correct,
            existing=matrix_object_repo.list_cells(db, config.id),
            exclude_id=cell.id,
        )
        if sibling is not None:
            raise _correct_conflict_error(config.selection_rule, sibling)
        for field, value in changes.items():
            setattr(cell, field, value)
        db.flush()
        audit_service.log_event(
            db, actor_user_id=user.id, action=audit_service.MATRIX_CELL_UPDATED,
            entity_type="MatrixCellDef", entity_id=cell.id, metadata=changes,
        )
        db.commit()
    db.refresh(cell)
    return MatrixCellDefRead.model_validate(cell)


def delete_cell(db: Session, cell_id: uuid.UUID, user: User) -> None:
    cell = matrix_object_repo.get_cell(db, cell_id)
    if cell is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matrix cell not found")
    config = _config_of(db, cell.matrix_object_config_id)
    _authorize_via_config(db, config, user, require_draft=True)
    cell_id_val = cell.id
    matrix_object_repo.delete_cell(db, cell)
    audit_service.log_event(
        db, actor_user_id=user.id, action=audit_service.MATRIX_CELL_DELETED,
        entity_type="MatrixCellDef", entity_id=cell_id_val,
    )
    db.commit()
