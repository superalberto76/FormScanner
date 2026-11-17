# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.matrix_object_config import MatrixObjectConfig


class MatrixColDef(Base, TimestampMixin):
    """A column definition of a MatrixObjectConfig."""

    __tablename__ = "matrix_col_defs"
    __table_args__ = (
        UniqueConstraint(
            "matrix_object_config_id",
            "order_index",
            name="uq_matrix_col_order",
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
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)

    # Relationships
    matrix_config: Mapped[MatrixObjectConfig] = relationship(
        "MatrixObjectConfig", back_populates="cols"
    )
