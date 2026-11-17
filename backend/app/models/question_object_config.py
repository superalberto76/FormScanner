# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum as SAEnum, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import QuestionType
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.page_object import PageObject
    from app.models.question_option import QuestionOption


class QuestionObjectConfig(Base, TimestampMixin):
    """Typed config for a QUESTION PageObject — single/multiple choice question."""

    __tablename__ = "question_object_configs"

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
    question_type: Mapped[QuestionType] = mapped_column(
        SAEnum(QuestionType, name="question_type_enum"), nullable=False
    )
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    shuffle_answers: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    # Relationships
    page_object: Mapped[PageObject] = relationship(
        "PageObject", back_populates="question_config"
    )
    options: Mapped[list[QuestionOption]] = relationship(
        "QuestionOption",
        back_populates="question_config",
        cascade="all, delete-orphan",
        order_by="QuestionOption.order_index",
    )
