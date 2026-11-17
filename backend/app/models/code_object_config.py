# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum as SAEnum, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import CodeType
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.page_object import PageObject


class CodeObjectConfig(Base, TimestampMixin):
    """Typed config for a CODE PageObject — QR / barcode to render.

    Reuses the ``CodeType`` enum (QR | BARCODE) rather than duplicating it,
    since the value space is identical.
    """

    __tablename__ = "code_object_configs"

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
    code_type: Mapped[CodeType] = mapped_column(
        SAEnum(CodeType, name="code_object_code_type_enum"), nullable=False
    )
    # Optional encoding hint (e.g. "UTF-8", "CODE_128").
    encoding: Mapped[str | None] = mapped_column(String(100), nullable=True)
    value: Mapped[str] = mapped_column(String(2000), nullable=False)
    human_readable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # "Include page number" (Identification): opt this CODE into
    # carrying the page index in its encoded payload. Only meaningful on the code
    # holding the VARIANT_IDENTIFIER role (enforced in code_object_service); the
    # export appends "|<page_index>" to the value on each page when True.
    include_page_index: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    # Relationships
    page_object: Mapped[PageObject] = relationship(
        "PageObject", back_populates="code_config"
    )
