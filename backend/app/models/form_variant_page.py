# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.mixins import CreatedAtMixin

if TYPE_CHECKING:
    from app.models.form_variant import FormVariant
    from app.models.page_object import PageObject


class FormVariantPage(Base, CreatedAtMixin):
    """A single page of a FormVariant.

    Geometry fields are nullable because, in the authoring domain, a page may
    be defined before a backing image is rendered/imported.
    """

    __tablename__ = "form_variant_pages"
    __table_args__ = (
        UniqueConstraint(
            "form_variant_id", "page_index", name="uq_form_variant_page_index"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    form_variant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("form_variants.id"),
        nullable=False,
        # The unique constraint (form_variant_id, page_index) already provides
        # a composite index whose prefix covers single-column lookups.
    )
    # 1-BASED index:
    # pages are provisioned and renumbered as 1..n by form_variant_page_repo
    # (FIRST_PAGE_INDEX / next_page_index / set_page_order), and the PDF export
    # encodes this value verbatim into a CODE payload (``value|<page_index>``),
    # so a scanned page's decoded index is 1-based too.
    page_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # Rendered background image, if any (null while purely authored).
    image_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    width_px: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height_px: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dpi: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Relationships
    form_variant: Mapped[FormVariant] = relationship(
        "FormVariant", back_populates="pages"
    )
    page_objects: Mapped[list[PageObject]] = relationship(
        "PageObject",
        back_populates="form_variant_page",
        cascade="all, delete-orphan",
        order_by="PageObject.z_index",
    )

    def __repr__(self) -> str:
        return f"<FormVariantPage id={self.id} idx={self.page_index}>"
