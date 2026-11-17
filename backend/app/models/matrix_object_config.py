# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum as SAEnum, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import MatrixSelectionRule
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.matrix_cell_def import MatrixCellDef
    from app.models.matrix_col_def import MatrixColDef
    from app.models.matrix_row_def import MatrixRowDef
    from app.models.page_object import PageObject


class MatrixObjectConfig(Base, TimestampMixin):
    """Typed config for a MATRIX PageObject — a grid of selectable cells.

    Reuses the ``MatrixSelectionRule`` enum. Rows/cols/cells are modelled as
    first-class child rows (not as JSON label arrays) so per-cell scoring is
    possible.
    """

    __tablename__ = "matrix_object_configs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # unique=True enforces the one-to-one relationship at DB level.
    page_object_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("page_objects.id"),
        nullable=False,
        unique=True,
    )
    rows_count: Mapped[int] = mapped_column(Integer, nullable=False)
    cols_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # When True, the matrix contributes to score computation.
    evaluative: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    selection_rule: Mapped[MatrixSelectionRule] = mapped_column(
        SAEnum(MatrixSelectionRule, name="matrix_object_selection_rule_enum"),
        nullable=False,
    )

    # Relationships
    page_object: Mapped[PageObject] = relationship(
        "PageObject", back_populates="matrix_config"
    )
    rows: Mapped[list[MatrixRowDef]] = relationship(
        "MatrixRowDef",
        back_populates="matrix_config",
        cascade="all, delete-orphan",
        order_by="MatrixRowDef.order_index",
    )
    cols: Mapped[list[MatrixColDef]] = relationship(
        "MatrixColDef",
        back_populates="matrix_config",
        cascade="all, delete-orphan",
        order_by="MatrixColDef.order_index",
    )
    cells: Mapped[list[MatrixCellDef]] = relationship(
        "MatrixCellDef",
        back_populates="matrix_config",
        cascade="all, delete-orphan",
    )
