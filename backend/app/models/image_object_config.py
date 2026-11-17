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


class ImageObjectConfig(Base, TimestampMixin):
    """Typed config for an IMAGE PageObject — a user-uploaded picture.

    The binary itself lives in the storage abstraction (``storage_service``); the
    config holds only a server-managed reference and presentation metadata. The
    object is created first (with just ``fit``) and the file is uploaded in a
    second step (POST /page-objects/{id}/image), so ``file_path`` and the file
    metadata are nullable until the upload populates them.
    """

    __tablename__ = "image_object_configs"

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
    # Absolute path managed by storage_service (NEVER a client-supplied path).
    # Null while the IMAGE object exists but no file has been uploaded yet.
    file_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # Original (client) filename, kept for display/download only — never used to
    # build the on-disk path.
    original_filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Real (content-sniffed) MIME type — one of the IMAGE whitelist.
    mime_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Intrinsic pixel dimensions (raster only; None for SVG / before upload).
    width_px: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height_px: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # How the picture fills its object area: "contain" (default) or "cover".
    fit: Mapped[str] = mapped_column(String(20), nullable=False, default="contain")

    # Relationships
    page_object: Mapped[PageObject] = relationship(
        "PageObject", back_populates="image_config"
    )

    @property
    def has_image(self) -> bool:
        """True once a file has been uploaded (used by the read schema)."""
        return self.file_path is not None
