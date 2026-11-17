# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    String,
    and_,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, foreign, mapped_column, relationship

from app.models.base import Base
from app.models.enums import PageObjectRole, PageObjectType
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.code_object_config import CodeObjectConfig
    from app.models.form_variant_page import FormVariantPage
    from app.models.form_variant_role_binding import FormVariantRoleBinding
    from app.models.free_text_area_config import FreeTextAreaConfig
    from app.models.image_object_config import ImageObjectConfig
    from app.models.matrix_object_config import MatrixObjectConfig
    from app.models.question_object_config import QuestionObjectConfig
    from app.models.submission_object_result import SubmissionObjectResult
    from app.models.text_object_config import TextObjectConfig


class PageObject(Base, TimestampMixin):
    """Level 3 — a generic graphical object placed on a FormVariantPage.

    The ``object_type`` discriminator selects which typed config table holds
    the object-specific data. Each PageObject has at most ONE typed config
    (one-to-one); object-specific data deliberately lives in typed tables
    rather than an opaque JSON blob.
    """

    __tablename__ = "page_objects"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    form_variant_page_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("form_variant_pages.id"),
        nullable=False,
        index=True,
    )
    object_type: Mapped[PageObjectType] = mapped_column(
        SAEnum(PageObjectType, name="page_object_type_enum"),
        nullable=False,
        index=True,
    )
    # Geometry — float coordinates (page/layout units), top-left origin.
    x: Mapped[float] = mapped_column(Float, nullable=False)
    y: Mapped[float] = mapped_column(Float, nullable=False)
    width: Mapped[float] = mapped_column(Float, nullable=False)
    height: Mapped[float] = mapped_column(Float, nullable=False)
    z_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Shared "template page" object.
    #   shared_group_id NULL                       → ordinary, page-local object;
    #   group_id set + shared_across_pages = True  → a LINKED member of the group
    #                                                (the per-page copies of one
    #                                                "show on every page" object);
    #   group_id set + shared_across_pages = False → a DETACHED copy that still
    #                                                remembers its origin group.
    # Group ids are fresh UUIDs and never reused across variants (the clone path
    # remaps them), so a query by shared_group_id is variant-local in practice.
    shared_group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    shared_across_pages: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    # Relationships
    form_variant_page: Mapped[FormVariantPage] = relationship(
        "FormVariantPage", back_populates="page_objects"
    )

    # Typed configs — one-to-one. Exactly one is expected to be populated,
    # consistent with object_type. (Enforcement is a service-layer concern.)
    text_config: Mapped[TextObjectConfig | None] = relationship(
        "TextObjectConfig",
        back_populates="page_object",
        uselist=False,
        cascade="all, delete-orphan",
    )
    code_config: Mapped[CodeObjectConfig | None] = relationship(
        "CodeObjectConfig",
        back_populates="page_object",
        uselist=False,
        cascade="all, delete-orphan",
    )
    free_text_config: Mapped[FreeTextAreaConfig | None] = relationship(
        "FreeTextAreaConfig",
        back_populates="page_object",
        uselist=False,
        cascade="all, delete-orphan",
    )
    question_config: Mapped[QuestionObjectConfig | None] = relationship(
        "QuestionObjectConfig",
        back_populates="page_object",
        uselist=False,
        cascade="all, delete-orphan",
    )
    matrix_config: Mapped[MatrixObjectConfig | None] = relationship(
        "MatrixObjectConfig",
        back_populates="page_object",
        uselist=False,
        cascade="all, delete-orphan",
    )
    image_config: Mapped[ImageObjectConfig | None] = relationship(
        "ImageObjectConfig",
        back_populates="page_object",
        uselist=False,
        cascade="all, delete-orphan",
    )

    # Scanning — detection results referencing this object.
    submission_results: Mapped[list[SubmissionObjectResult]] = relationship(
        "SubmissionObjectResult",
        back_populates="page_object",
    )

    # Optional special scanning role (Identification). Lives in a
    # first-class binding table, never as a column here. No cascade: an object
    # that still holds a role cannot be deleted (blocked in the service layer).
    role_binding: Mapped[FormVariantRoleBinding | None] = relationship(
        "FormVariantRoleBinding",
        back_populates="page_object",
        uselist=False,
    )

    # ``group_role_bindings`` — the role bindings of every LINKED member of this
    # object's shared group, this row included when it holds one.
    # Read-only; mapped below the class (``_map_group_role_bindings``), because
    # it joins page_objects to an alias of itself. Empty for an object outside
    # any group and for a detached copy.

    @property
    def role(self) -> PageObjectRole | None:
        """Convenience view of the special role (or None). Lets the read models
        expose ``role`` via from_attributes without a dedicated query.

        The row's OWN binding: what authoring assigns and removes. Reading and
        reviewing a sheet go through :attr:`effective_role` instead."""
        return self.role_binding.role if self.role_binding else None

    @property
    def effective_role(self) -> PageObjectRole | None:
        """The role this object plays on a scanned sheet.

        A "show on every page" object is one row per page, but its binding
        lives on ONE of them: every LINKED copy carries the group's role, for
        both roles and every object type. A detached copy is no
        longer that object and has only its own binding, like an ordinary one.

        The row's own binding wins; otherwise the group's. Two copies of one
        group holding different roles is pathological (nothing in authoring
        produces it on purpose): the copy on the lowest page decides, like the
        export's render order.
        """
        if self.role_binding is not None:
            return self.role_binding.role
        bindings = self.group_role_bindings
        if not bindings:
            return None
        if len(bindings) == 1:
            return bindings[0].role
        # Pathological case only: ``page_index`` loads each binding's object
        # and page lazily, which is not worth eager-loading for.
        return min(bindings, key=lambda b: b.page_index).role

    def __repr__(self) -> str:
        return f"<PageObject id={self.id} type={self.object_type}>"


def _map_group_role_bindings() -> None:
    from app.models.form_variant_role_binding import FormVariantRoleBinding

    sibling = PageObject.__table__.alias("linked_sibling")
    PageObject.group_role_bindings = relationship(
        FormVariantRoleBinding,
        secondary=sibling,
        primaryjoin=and_(
            PageObject.shared_across_pages.is_(True),
            PageObject.shared_group_id == foreign(sibling.c.shared_group_id),
            sibling.c.shared_across_pages.is_(True),
        ),
        secondaryjoin=FormVariantRoleBinding.page_object_id == foreign(sibling.c.id),
        viewonly=True,
    )


_map_group_role_bindings()
