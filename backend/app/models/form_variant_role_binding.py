# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Enum as SAEnum, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import PageObjectRole
from app.models.mixins import CreatedAtMixin

if TYPE_CHECKING:
    from app.models.page_object import PageObject


class FormVariantRoleBinding(Base, CreatedAtMixin):
    """Binds a PageObject to a special scanning role within its FormVariant
    (Identification).

    A first-class link table (NOT a column on ``page_objects``): the object it
    points at keeps its plain shape, and the two DB-level invariants live here —
      * ``(form_variant_id, role)`` unique → at most ONE binding per role per
        variant (one VARIANT_IDENTIFIER, one RESPONDENT_IDENTIFIER);
      * ``page_object_id`` unique → an object can never hold two roles at once.
    Append-only ``CreatedAtMixin`` (a binding is only ever created or deleted,
    never edited). The FK to ``page_objects`` deliberately has NO ON DELETE
    CASCADE: deleting an object that still holds a role is blocked in the service
    layer (page_object_service.delete_object), not silently cascaded away.
    """

    __tablename__ = "form_variant_role_bindings"
    __table_args__ = (
        UniqueConstraint(
            "form_variant_id", "role", name="uq_form_variant_role_binding_role"
        ),
        UniqueConstraint(
            "page_object_id", name="uq_form_variant_role_binding_object"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    form_variant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("form_variants.id"),
        nullable=False,
        index=True,
    )
    page_object_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("page_objects.id"),
        nullable=False,
        # No separate index: the unique constraint on page_object_id already
        # covers the by-object lookup.
    )
    role: Mapped[PageObjectRole] = mapped_column(
        SAEnum(PageObjectRole, name="page_object_role_enum"),
        nullable=False,
    )

    # One-to-one back to the object that carries the role. No delete cascade:
    # the service blocks deleting an object that still holds a binding.
    page_object: Mapped[PageObject] = relationship(
        "PageObject", back_populates="role_binding"
    )

    @property
    def object_label(self) -> str | None:
        """The bound object's label, verbatim (may be None — no text fallback).
        Lets GET /form-variants/{id}/role-bindings render "object + page" in the
        Identification summary without a per-object fetch. Same spirit as
        PageObject.role, in the inverse direction."""
        return self.page_object.label

    @property
    def page_index(self) -> int:
        """The 0-based index of the page that hosts the bound object."""
        return self.page_object.form_variant_page.page_index

    @property
    def shared_group_id(self) -> uuid.UUID | None:
        """The bound object's shared "template page" group id, or
        None for an ordinary page-local object. Lets the FE tell whether a shared
        sibling (same group) holds VARIANT_IDENTIFIER — mirroring
        code_object_service._validate_include_page_index — to enable
        the "include page number" toggle in preview, without waiting for a 409.
        Read straight off the already-loaded page_object row (no extra join)."""
        return self.page_object.shared_group_id

    def __repr__(self) -> str:
        return (
            f"<FormVariantRoleBinding form_variant_id={self.form_variant_id} "
            f"page_object_id={self.page_object_id} role={self.role}>"
        )
