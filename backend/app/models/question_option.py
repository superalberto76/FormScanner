# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Float, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.question_object_config import QuestionObjectConfig


class QuestionOption(Base, TimestampMixin):
    """A selectable option of a QuestionObjectConfig."""

    __tablename__ = "question_options"
    __table_args__ = (
        UniqueConstraint(
            "question_object_config_id",
            "order_index",
            name="uq_question_option_order",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    question_object_config_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("question_object_configs.id"),
        nullable=False,
        index=True,
    )
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    option_text: Mapped[str] = mapped_column(Text, nullable=False)
    is_correct: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Optional per-option score; may be negative (penalty for a distractor).
    score: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Relationships
    question_config: Mapped[QuestionObjectConfig] = relationship(
        "QuestionObjectConfig", back_populates="options"
    )
