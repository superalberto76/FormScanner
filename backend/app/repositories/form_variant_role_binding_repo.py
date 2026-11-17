# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Data-access layer for FormVariantRoleBinding (identification roles).

Thin query layer over the ``form_variant_role_bindings`` link table; the role
invariants (allowed object types, one-role-per-object, one-object-per-role) are
enforced in ``page_object_role_service``.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models.enums import PageObjectRole
from app.models.form_variant_role_binding import FormVariantRoleBinding
from app.models.page_object import PageObject


def add(db: Session, binding: FormVariantRoleBinding) -> FormVariantRoleBinding:
    db.add(binding)
    db.flush()  # populate binding.id
    return binding


def delete(db: Session, binding: FormVariantRoleBinding) -> None:
    db.delete(binding)
    db.flush()


def get_by_page_object(
    db: Session, page_object_id: uuid.UUID
) -> FormVariantRoleBinding | None:
    """The role binding held by *page_object_id*, or None (at most one)."""
    stmt = select(FormVariantRoleBinding).where(
        FormVariantRoleBinding.page_object_id == page_object_id
    )
    return db.scalars(stmt).first()


def get_by_variant_and_role(
    db: Session, form_variant_id: uuid.UUID, role: PageObjectRole
) -> FormVariantRoleBinding | None:
    """The binding for (*form_variant_id*, *role*), or None (at most one)."""
    stmt = select(FormVariantRoleBinding).where(
        FormVariantRoleBinding.form_variant_id == form_variant_id,
        FormVariantRoleBinding.role == role,
    )
    return db.scalars(stmt).first()


def find_variant_with_role_among(
    db: Session, form_variant_ids: list[uuid.UUID], role: PageObjectRole
) -> uuid.UUID | None:
    """The form_variant_id among *form_variant_ids* that holds a binding for
    *role*, or None if no such binding exists. At most one match is possible
    (role is unique per variant).

    A single ``IN`` query rather than N per-variant lookups — backs the
    scan-readiness check over a project's PUBLISHED variants.
    """
    if not form_variant_ids:
        return None
    stmt = select(FormVariantRoleBinding.form_variant_id).where(
        FormVariantRoleBinding.form_variant_id.in_(form_variant_ids),
        FormVariantRoleBinding.role == role,
    )
    return db.scalars(stmt).first()


def variants_with_role(
    db: Session, form_variant_ids: set[uuid.UUID], role: PageObjectRole
) -> set[uuid.UUID]:
    """Those of *form_variant_ids* holding a binding for *role*, in ONE query.

    The plural of :func:`find_variant_with_role_among`, which stops at the first
    match: the batch detail needs the answer for EVERY variant its submissions
    matched, to say which of them expect a candidate identifier.
    Empty input returns an empty set without touching the database.
    """
    if not form_variant_ids:
        return set()
    stmt = select(FormVariantRoleBinding.form_variant_id).where(
        FormVariantRoleBinding.form_variant_id.in_(form_variant_ids),
        FormVariantRoleBinding.role == role,
    )
    return set(db.scalars(stmt).all())


def list_for_variant(
    db: Session, form_variant_id: uuid.UUID
) -> list[FormVariantRoleBinding]:
    """The current bindings of a variant (0, 1 or 2 rows), oldest first.

    Eager-loads the object and its hosting page in one query so the
    ``object_label`` / ``page_index`` read-only properties serialize without an
    N+1 on GET /form-variants/{id}/role-bindings.
    """
    stmt = (
        select(FormVariantRoleBinding)
        .where(FormVariantRoleBinding.form_variant_id == form_variant_id)
        .options(
            joinedload(FormVariantRoleBinding.page_object).joinedload(
                PageObject.form_variant_page
            )
        )
        .order_by(FormVariantRoleBinding.created_at)
    )
    return list(db.scalars(stmt).all())
