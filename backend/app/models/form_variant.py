# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import FormVariantStatus
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.form_project import FormProject
    from app.models.form_variant_page import FormVariantPage
    from app.models.submission import Submission
    from app.models.user import User


class FormVariant(Base, TimestampMixin):
    """Level 2 — a printable, versionable/publishable variant of a FormProject.

    A project may have several variants (Test A / Test B / …); each is
    independently versioned and published.
    """

    __tablename__ = "form_variants"
    __table_args__ = (
        UniqueConstraint(
            "form_project_id",
            "version_number",
            name="uq_form_variant_version_number",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    form_project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("form_projects.id"),
        nullable=False,
        index=True,
    )
    # Stable grouping key shared across versions of the same logical variant.
    root_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    status: Mapped[FormVariantStatus] = mapped_column(
        SAEnum(FormVariantStatus, name="form_variant_status_enum"),
        nullable=False,
        default=FormVariantStatus.DRAFT,
        index=True,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # Original uploaded source (PDF/image), if the variant was imported.
    original_file_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    original_filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )

    # Relationships
    form_project: Mapped[FormProject] = relationship(
        "FormProject", back_populates="variants"
    )
    pages: Mapped[list[FormVariantPage]] = relationship(
        "FormVariantPage",
        back_populates="form_variant",
        cascade="all, delete-orphan",
        order_by="FormVariantPage.page_index",
    )
    submissions: Mapped[list[Submission]] = relationship(
        "Submission",
        back_populates="matched_form_variant",
    )
    # Two FKs to users.id — foreign_keys must be specified explicitly.
    created_by_user: Mapped[User] = relationship(
        "User", foreign_keys=[created_by_user_id]
    )
    updated_by_user: Mapped[User | None] = relationship(
        "User", foreign_keys=[updated_by_user_id]
    )

    def __repr__(self) -> str:
        return (
            f"<FormVariant id={self.id} "
            f"v={self.version_number} status={self.status}>"
        )
