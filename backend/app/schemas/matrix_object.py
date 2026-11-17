# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pydantic schemas for MATRIX PageObjects."""
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import MatrixSelectionRule, PageObjectType
from app.schemas.page_object import PageObjectBaseRead, PageObjectCreateBase


# ---------------------------------------------------------------------------
# MatrixRowDef
# ---------------------------------------------------------------------------


class MatrixRowDefBase(BaseModel):
    order_index: int = Field(ge=0)
    label: str


class MatrixRowDefCreate(MatrixRowDefBase):
    pass


class MatrixRowDefUpdate(BaseModel):
    order_index: int | None = Field(default=None, ge=0)
    label: str | None = None


class MatrixRowDefRead(MatrixRowDefBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    matrix_object_config_id: uuid.UUID
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# MatrixColDef
# ---------------------------------------------------------------------------


class MatrixColDefBase(BaseModel):
    order_index: int = Field(ge=0)
    label: str


class MatrixColDefCreate(MatrixColDefBase):
    pass


class MatrixColDefUpdate(BaseModel):
    order_index: int | None = Field(default=None, ge=0)
    label: str | None = None


class MatrixColDefRead(MatrixColDefBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    matrix_object_config_id: uuid.UUID
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# MatrixCellDef
# ---------------------------------------------------------------------------


class MatrixCellDefBase(BaseModel):
    row_index: int = Field(ge=0)
    col_index: int = Field(ge=0)
    is_correct: bool = False
    # May be negative.
    score: float | None = None


class MatrixCellDefCreate(MatrixCellDefBase):
    pass


class MatrixCellDefUpdate(BaseModel):
    row_index: int | None = Field(default=None, ge=0)
    col_index: int | None = Field(default=None, ge=0)
    is_correct: bool | None = None
    score: float | None = None


class MatrixCellDefRead(MatrixCellDefBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    matrix_object_config_id: uuid.UUID
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# MatrixObjectConfig
# ---------------------------------------------------------------------------


class MatrixObjectConfigBase(BaseModel):
    rows_count: int = Field(ge=1)
    cols_count: int = Field(ge=1)
    evaluative: bool = False
    selection_rule: MatrixSelectionRule


class MatrixObjectConfigCreate(MatrixObjectConfigBase):
    rows: list[MatrixRowDefCreate] = Field(default_factory=list)
    cols: list[MatrixColDefCreate] = Field(default_factory=list)
    cells: list[MatrixCellDefCreate] = Field(default_factory=list)


class MatrixObjectConfigUpdate(BaseModel):
    # rows/cols/cells are managed via the dedicated /matrix-* endpoints.
    rows_count: int | None = Field(default=None, ge=1)
    cols_count: int | None = Field(default=None, ge=1)
    evaluative: bool | None = None
    selection_rule: MatrixSelectionRule | None = None


class MatrixObjectConfigRead(MatrixObjectConfigBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    page_object_id: uuid.UUID
    rows: list[MatrixRowDefRead] = Field(default_factory=list)
    cols: list[MatrixColDefRead] = Field(default_factory=list)
    cells: list[MatrixCellDefRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# MATRIX typed create / read
# ---------------------------------------------------------------------------


class MatrixPageObjectCreate(PageObjectCreateBase):
    config: MatrixObjectConfigCreate


class MatrixPageObjectRead(PageObjectBaseRead):
    object_type: Literal[PageObjectType.MATRIX] = PageObjectType.MATRIX
    matrix_config: MatrixObjectConfigRead
