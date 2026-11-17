# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Float, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.matrix_object_config import MatrixObjectConfig


class MatrixCellDef(Base, TimestampMixin):
    """A single (row, col) cell definition of a MatrixObjectConfig.

    Only cells that carry data (e.g. a non-default score) need to exist; the
    unique (config, row, col) constraint prevents duplicates.
    """

    __tablename__ = "matrix_cell_defs"
    __table_args__ = (
        UniqueConstraint(
            "matrix_object_config_id",
            "row_index",
            "col_index",
            name="uq_matrix_cell_coords",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    matrix_object_config_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("matrix_object_configs.id"),
        nullable=False,
        index=True,
    )
    row_index: Mapped[int] = mapped_column(Integer, nullable=False)
    col_index: Mapped[int] = mapped_column(Integer, nullable=False)
    is_correct: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Optional per-cell score; may be negative.
    score: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Relationships
    matrix_config: Mapped[MatrixObjectConfig] = relationship(
        "MatrixObjectConfig", back_populates="cells"
    )
