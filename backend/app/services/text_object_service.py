# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""TEXT PageObject service."""
from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.enums import PageObjectType
from app.models.page_object import PageObject
from app.models.text_object_config import TextObjectConfig
from app.models.user import User
from app.repositories import page_object_repo, text_object_repo
from app.schemas.page_object import (
    TextObjectConfigUpdate,
    TextPageObjectCreate,
)
from app.services import audit_service, page_object_service


def create_text(
    db: Session, page_id: uuid.UUID, data: TextPageObjectCreate, user: User
):
    page, _ = page_object_service.load_page_authorized(
        db, page_id, user, require_draft=True
    )
    # Reserved bands: nothing is placed in them.
    page_object_service.assert_outside_bands(data, page)

    obj = PageObject(
        form_variant_page_id=page.id,
        object_type=PageObjectType.TEXT,
        x=data.x, y=data.y, width=data.width, height=data.height,
        z_index=data.z_index, label=data.label, description=data.description,
        visible=data.visible, locked=data.locked,
        # "Show on all pages": a fresh linked group, this object its
        # template. Existing pages aren't copied to; new pages get a linked copy.
        shared_group_id=uuid.uuid4() if data.shared_across_pages else None,
        shared_across_pages=data.shared_across_pages,
    )
    page_object_repo.add(db, obj)

    cfg = data.config
    text_object_repo.add(
        db,
        TextObjectConfig(
            page_object_id=obj.id,
            text=cfg.text,
            font_size=cfg.font_size,
            font_family=cfg.font_family,
            bold=cfg.bold,
            italic=cfg.italic,
        ),
    )

    audit_service.log_event(
        db, actor_user_id=user.id, action=audit_service.PAGE_OBJECT_CREATED,
        entity_type="PageObject", entity_id=obj.id, metadata={"object_type": "TEXT"},
    )
    db.commit()
    db.refresh(obj)
    return page_object_service.build_typed_read(obj)


def update_text_config(
    db: Session, object_id: uuid.UUID, data: TextObjectConfigUpdate, user: User
):
    obj, _ = page_object_service.load_object_authorized(
        db, object_id, user, require_draft=True
    )
    page_object_service.assert_object_type(obj, PageObjectType.TEXT)

    config = obj.text_config
    if config is None:  # defensive — every TEXT object has a config
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Text config not found"
        )

    changes = data.model_dump(exclude_unset=True)
    if changes:
        for field, value in changes.items():
            setattr(config, field, value)
        db.flush()
        audit_service.log_event(
            db, actor_user_id=user.id,
            action=audit_service.PAGE_OBJECT_CONFIG_UPDATED,
            entity_type="TextObjectConfig", entity_id=config.id, metadata=changes,
        )
        db.commit()
    db.refresh(obj)
    return page_object_service.build_typed_read(obj)
