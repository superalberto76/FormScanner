# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.page_object import PageObject


class TextObjectConfig(Base, TimestampMixin):
    """Typed config for a TEXT PageObject — static rendered text."""

    __tablename__ = "text_object_configs"

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
    text: Mapped[str] = mapped_column(Text, nullable=False)
    font_size: Mapped[int] = mapped_column(Integer, nullable=False, default=12)
    font_family: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bold: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    italic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Relationships
    page_object: Mapped[PageObject] = relationship(
        "PageObject", back_populates="text_config"
    )
