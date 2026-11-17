# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.page_object import PageObject


class FreeTextAreaConfig(Base, TimestampMixin):
    """Typed config for a FREE_TEXT_AREA PageObject — handwritten/typed answer."""

    __tablename__ = "free_text_area_configs"

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
    placeholder: Mapped[str | None] = mapped_column(String(500), nullable=True)
    max_length: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Relationships
    page_object: Mapped[PageObject] = relationship(
        "PageObject", back_populates="free_text_config"
    )
