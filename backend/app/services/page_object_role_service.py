# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""PageObject special-role service (identification).

Assign / remove / list the optional scanning roles VARIANT_IDENTIFIER and
RESPONDENT_IDENTIFIER, stored as first-class ``FormVariantRoleBinding`` rows.

Authorization and the DRAFT gate are reused verbatim from page_object_service
(``load_object_authorized(..., require_draft=True)``): a role is authored content,
so every mutation requires the parent FormVariant to be DRAFT. Kept in its own
module because page_object_service is already large; it depends on that module
(not the reverse), so there is no import cycle.
"""
from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.enums import CodeEncoding, CodeType, PageObjectRole, PageObjectType
from app.models.form_variant_role_binding import FormVariantRoleBinding
from app.models.user import User
from app.repositories import form_variant_repo, form_variant_role_binding_repo
from app.services import audit_service, page_object_service
from app.services.form_project_service import require_project_member

# Which object types may carry each role. VARIANT_IDENTIFIER
# is a single code (QR/barcode); RESPONDENT_IDENTIFIER may also be handwritten
# (FREE_TEXT_AREA) or bubbled (MATRIX).
_ALLOWED_TYPES_BY_ROLE: dict[PageObjectRole, frozenset[PageObjectType]] = {
    PageObjectRole.VARIANT_IDENTIFIER: frozenset({PageObjectType.CODE}),
    PageObjectRole.RESPONDENT_IDENTIFIER: frozenset(
        {PageObjectType.CODE, PageObjectType.MATRIX, PageObjectType.FREE_TEXT_AREA}
    ),
}


def assign_role(
    db: Session, object_id: uuid.UUID, role: PageObjectRole, user: User
):
    """Assign a special role to an object (DRAFT only).

    409 if: the object's type is not allowed for the role; a respondent-ID grid
    (RESPONDENT_IDENTIFIER on a MATRIX) has a row label that is blank or holds
    "?"; the object already holds a role (any); or the role is
    already held by another object of the same variant (the message names that
    object so the user can free it first).
    Returns the object's typed read (now exposing ``role``).
    """
    obj, variant = page_object_service.load_object_authorized(
        db, object_id, user, require_draft=True
    )

    # 1) The object's type must be admissible for the requested role.
    if obj.object_type not in _ALLOWED_TYPES_BY_ROLE[role]:
        allowed = ", ".join(
            sorted(t.value for t in _ALLOWED_TYPES_BY_ROLE[role])
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Object {obj.id} is {obj.object_type.value}; role "
                f"{role.value} can only be assigned to: {allowed}."
            ),
        )

    # 1a) A barcode can carry a role only as CODE128: the role tag
    #     ("VID:"/"RID:") cannot be encoded in CODE39, EAN or UPC. A barcode
    #     with no encoding is printed as Code128 by the export and shown as
    #     CODE128 by the FE, so it counts as CODE128 here too.
    cfg = obj.code_config if obj.object_type is PageObjectType.CODE else None
    if (
        cfg is not None
        and cfg.code_type is CodeType.BARCODE
        and (cfg.encoding or CodeEncoding.CODE128.value) != CodeEncoding.CODE128.value
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Role {role.value} can only be assigned to a barcode with the "
                "CODE128 encoding."
            ),
        )

    # 1b) VARIANT_IDENTIFIER must live on an object shared across all pages:
    #     its optional "include page number" payload only makes sense when
    #     the identifier code is replicated onto every page. This
    #     requirement is specific to VARIANT_IDENTIFIER — RESPONDENT_IDENTIFIER
    #     carries no such rule.
    if role is PageObjectRole.VARIANT_IDENTIFIER and not (
        obj.shared_across_pages and obj.shared_group_id is not None
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Share this object across all pages before assigning the "
                "VARIANT_IDENTIFIER role."
            ),
        )

    # 1c) A respondent-ID grid reads each column as the label of its marked
    #     row: every defined label must be usable as that character. A row
    #     with no definition reads as its index, never blank.
    if role is PageObjectRole.RESPONDENT_IDENTIFIER:
        page_object_service.check_identifier_row_labels(obj)

    # 2) One role per object: block if this object already holds one.
    existing_for_object = form_variant_role_binding_repo.get_by_page_object(
        db, obj.id
    )
    if existing_for_object is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Object already has the role {existing_for_object.role.value} "
                "assigned; remove it before assigning a new one."
            ),
        )

    # 3) One object per role, per variant: block if another object already holds
    #    this role, naming it so the caller knows what to free first.
    existing_for_role = form_variant_role_binding_repo.get_by_variant_and_role(
        db, variant.id, role
    )
    if existing_for_role is not None:
        holder = existing_for_role.page_object
        holder_label = f" ('{holder.label}')" if holder.label else ""
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Role {role.value} is already held by object "
                f"{existing_for_role.page_object_id}{holder_label} in this "
                "variant; remove it there before assigning it here."
            ),
        )

    form_variant_role_binding_repo.add(
        db,
        FormVariantRoleBinding(
            form_variant_id=variant.id,
            page_object_id=obj.id,
            role=role,
        ),
    )
    audit_service.log_event(
        db,
        actor_user_id=user.id,
        action=audit_service.PAGE_OBJECT_ROLE_ASSIGNED,
        entity_type="PageObject",
        entity_id=obj.id,
        metadata={"role": role.value},
    )
    db.commit()
    db.refresh(obj)
    return page_object_service.build_typed_read(obj)


def remove_role(db: Session, object_id: uuid.UUID, user: User) -> None:
    """Remove the object's role binding (DRAFT only). 404 if it has none."""
    obj, _ = page_object_service.load_object_authorized(
        db, object_id, user, require_draft=True
    )

    binding = form_variant_role_binding_repo.get_by_page_object(db, obj.id)
    if binding is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Object has no role assigned",
        )

    role = binding.role
    form_variant_role_binding_repo.delete(db, binding)

    # Freeing a VARIANT_IDENTIFIER also clears the include_page_index option it
    # underpins: the flag may never stay active without the role
    # that justifies it. Scoped strictly to VARIANT_IDENTIFIER on a CODE object
    # whose flag is currently on — no other role or field is touched.
    include_page_index_reset = False
    if (
        role is PageObjectRole.VARIANT_IDENTIFIER
        and obj.object_type is PageObjectType.CODE
        and obj.code_config is not None
        and obj.code_config.include_page_index
    ):
        obj.code_config.include_page_index = False
        include_page_index_reset = True

    metadata = {"role": role.value}
    if include_page_index_reset:
        metadata["include_page_index_reset"] = True
    audit_service.log_event(
        db,
        actor_user_id=user.id,
        action=audit_service.PAGE_OBJECT_ROLE_REMOVED,
        entity_type="PageObject",
        entity_id=obj.id,
        metadata=metadata,
    )
    db.commit()


def list_variant_role_bindings(
    db: Session, form_variant_id: uuid.UUID, user: User
) -> list[FormVariantRoleBinding]:
    """The current role bindings of a variant (0, 1 or 2 rows).

    Read access mirrors form_variant_service's reads: member/admin on the
    variant's parent project (404 if the variant is missing, 403 if not a
    member).
    """
    variant = form_variant_repo.get(db, form_variant_id)
    if variant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Form variant not found"
        )
    require_project_member(db, variant.form_project_id, user)
    return form_variant_role_binding_repo.list_for_variant(db, form_variant_id)
