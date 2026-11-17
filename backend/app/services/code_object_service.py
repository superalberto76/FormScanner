# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CODE PageObject service."""
from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.code_object_config import CodeObjectConfig
from app.models.enums import CodeEncoding, CodeType, PageObjectRole, PageObjectType
from app.models.page_object import PageObject
from app.models.user import User
from app.repositories import (
    code_object_repo,
    form_variant_page_repo,
    form_variant_role_binding_repo,
    page_object_repo,
)
from app.schemas.page_object import CodeObjectConfigUpdate, CodePageObjectCreate
from app.services import audit_service, page_object_service
from app.utils.code_rendering import barcode_value_error

# Human-readable list of valid symbologies for the 422 detail message.
_VALID_ENCODINGS = ", ".join(e.value for e in CodeEncoding)


def _validate_code_encoding(code_type: CodeType, encoding) -> None:
    """Enforce the encoding⇄code_type contract; raise 422 on violation.

    QR codes carry no symbology (encoding must be empty); barcodes require a
    valid one. *encoding* may be a ``CodeEncoding``, its plain string value (as
    read back from the DB column), or ``None`` — all three are accepted.
    """
    if code_type is CodeType.QR:
        if encoding is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="QR codes do not use an encoding; leave it empty.",
            )
        return

    # BARCODE: encoding must be present and a recognised symbology. CodeEncoding()
    # accepts either a CodeEncoding member or its string value and raises
    # ValueError for None or anything unrecognised.
    try:
        CodeEncoding(encoding)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"A barcode requires a valid encoding (one of: {_VALID_ENCODINGS})."
            ),
        ) from None


def _validate_barcode_value(code_type: CodeType, encoding, value: str) -> None:
    """Reject a barcode value its symbology cannot encode (422).

    Runs after ``_validate_code_encoding``, so for a BARCODE *encoding* is a
    valid ``CodeEncoding`` (member or string value). The rules live in
    ``code_rendering.barcode_value_error``, shared with the export.
    """
    if code_type is not CodeType.BARCODE:
        return
    problem = barcode_value_error(CodeEncoding(encoding), value)
    if problem is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=problem
        )


def _validate_role_symbology(
    db: Session, obj: PageObject, code_type: CodeType, encoding
) -> None:
    """A barcode whose shared group holds a role must stay CODE128 (409).

    The role tag (``VID:``/``RID:``) and the page index cannot be
    encoded in CODE39, EAN or UPC. The role may sit on obj itself or on any
    member of its shared group, whose config resync makes identical. Same lookup
    as ``_validate_include_page_index``: the variant's bindings (at most two)
    against the group's ids.
    """
    if code_type is not CodeType.BARCODE or CodeEncoding(encoding) is CodeEncoding.CODE128:
        return
    page = form_variant_page_repo.get(db, obj.form_variant_page_id)
    if page is None:
        return
    member_ids = {obj.id}
    if obj.shared_group_id is not None:
        member_ids.update(
            m.id for m in page_object_repo.list_by_shared_group(db, obj.shared_group_id)
        )
    for binding in form_variant_role_binding_repo.list_for_variant(db, page.form_variant_id):
        if binding.page_object_id in member_ids:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"This barcode (or a linked copy) holds the {binding.role.value} "
                    "role, which requires the CODE128 encoding."
                ),
            )


def _validate_include_page_index(
    db: Session, obj: PageObject, include_page_index: bool
) -> None:
    """Gate the ``include_page_index`` flag on the VARIANT_IDENTIFIER role (409).

    Same contract as ``_validate_code_encoding``: validates the MERGED final
    state, so a no-op when the flag ends up False. When True, obj's variant must
    have a VARIANT_IDENTIFIER binding pointing at obj itself OR at a linked
    sibling sharing its ``shared_group_id`` — the page-index payload is only
    meaningful on the code that identifies the variant. At create
    time this practically always rejects: a brand-new object cannot yet hold a
    role, which is the intended behaviour.
    """
    if not include_page_index:
        return

    # Ascend obj -> page -> variant (same path as load_page_authorized).
    page = form_variant_page_repo.get(db, obj.form_variant_page_id)
    binding = None
    if page is not None:
        binding = form_variant_role_binding_repo.get_by_variant_and_role(
            db, page.form_variant_id, PageObjectRole.VARIANT_IDENTIFIER
        )

    # The role may sit on obj itself or on any member of its shared group.
    allowed_ids = {obj.id}
    if obj.shared_group_id is not None:
        allowed_ids.update(
            m.id
            for m in page_object_repo.list_by_shared_group(db, obj.shared_group_id)
        )

    if binding is None or binding.page_object_id not in allowed_ids:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "include_page_index requires this object (or a linked sibling) to "
                "hold the VARIANT_IDENTIFIER role first."
            ),
        )


def create_code(
    db: Session, page_id: uuid.UUID, data: CodePageObjectCreate, user: User
):
    page, _ = page_object_service.load_page_authorized(
        db, page_id, user, require_draft=True
    )
    # Reserved bands: nothing is placed in them.
    page_object_service.assert_outside_bands(data, page)

    cfg = data.config
    _validate_code_encoding(cfg.code_type, cfg.encoding)
    _validate_barcode_value(cfg.code_type, cfg.encoding, cfg.value)

    obj = PageObject(
        form_variant_page_id=page.id,
        object_type=PageObjectType.CODE,
        x=data.x, y=data.y, width=data.width, height=data.height,
        z_index=data.z_index, label=data.label, description=data.description,
        visible=data.visible, locked=data.locked,
        # "Show on all pages": a fresh linked group, this object its
        # template. Existing pages aren't copied to; new pages get a linked copy.
        shared_group_id=uuid.uuid4() if data.shared_across_pages else None,
        shared_across_pages=data.shared_across_pages,
    )
    # Validate BEFORE persisting: a just-created object cannot yet hold the
    # VARIANT_IDENTIFIER role, so include_page_index=True is (correctly) refused
    # here without leaving a flushed row behind.
    _validate_include_page_index(db, obj, cfg.include_page_index)
    page_object_repo.add(db, obj)

    code_object_repo.add(
        db,
        CodeObjectConfig(
            page_object_id=obj.id,
            code_type=cfg.code_type,
            encoding=cfg.encoding,
            value=cfg.value,
            human_readable=cfg.human_readable,
            include_page_index=cfg.include_page_index,
        ),
    )

    audit_service.log_event(
        db, actor_user_id=user.id, action=audit_service.PAGE_OBJECT_CREATED,
        entity_type="PageObject", entity_id=obj.id, metadata={"object_type": "CODE"},
    )
    db.commit()
    db.refresh(obj)
    return page_object_service.build_typed_read(obj)


def update_code_config(
    db: Session, object_id: uuid.UUID, data: CodeObjectConfigUpdate, user: User
):
    obj, _ = page_object_service.load_object_authorized(
        db, object_id, user, require_draft=True
    )
    page_object_service.assert_object_type(obj, PageObjectType.CODE)

    config = obj.code_config
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Code config not found"
        )

    changes = data.model_dump(exclude_unset=True)
    if changes:
        # Validate the encoding⇄code_type contract against the MERGED final
        # state, so a PATCH touching only one of the two fields is still checked.
        final_code_type = changes.get("code_type", config.code_type)
        final_encoding = changes.get("encoding", config.encoding)
        _validate_code_encoding(final_code_type, final_encoding)
        _validate_barcode_value(
            final_code_type, final_encoding, changes.get("value", config.value)
        )
        _validate_role_symbology(db, obj, final_code_type, final_encoding)
        final_include = changes.get(
            "include_page_index", config.include_page_index
        )
        _validate_include_page_index(db, obj, final_include)

        for field, value in changes.items():
            setattr(config, field, value)
        db.flush()
        audit_service.log_event(
            db, actor_user_id=user.id,
            action=audit_service.PAGE_OBJECT_CONFIG_UPDATED,
            entity_type="CodeObjectConfig", entity_id=config.id,
            metadata={k: (v.value if hasattr(v, "value") else v) for k, v in changes.items()},
        )
        db.commit()
    db.refresh(obj)
    return page_object_service.build_typed_read(obj)
