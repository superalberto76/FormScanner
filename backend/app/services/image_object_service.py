# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""IMAGE PageObject service.

Flow (deliberately mirrors the other typed-create services for everything but
the file, which storage cannot carry in JSON):

  1. ``create_image``      — POST .../objects/image (JSON: geometry + fit). The
     object + an empty ImageObjectConfig are created; no file yet.
  2. ``upload_image``      — POST /page-objects/{id}/image (multipart). The bytes
     are size-checked, content-sniffed (real MIME, never the declared one),
     SVGs are sanitized, the file is saved under a server-generated name and the
     config is populated.
  3. ``update_image_config`` — PATCH .../image-config (JSON: fit). Follows the
     single-Save draft model; the file upload is its own action.
  4. ``get_image_file``    — GET /page-objects/{id}/image. Returns bytes + MIME.

Authorization + DRAFT gating are reused from page_object_service, exactly like
the CODE/TEXT services.
"""
from __future__ import annotations

import uuid
from pathlib import PurePath

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.enums import PageObjectType
from app.models.image_object_config import ImageObjectConfig
from app.models.page_object import PageObject
from app.models.user import User
from app.repositories import image_object_repo, page_object_repo
from app.schemas.page_object import ImageObjectConfigUpdate, ImagePageObjectCreate
from app.services import audit_service, page_object_service, storage_service
from app.utils import image_validation, svg_sanitizer


def _display_filename(filename: str | None) -> str | None:
    """Sanitised, length-capped original filename for display/download only.

    Never used to build an on-disk path — only stored for the UI. Strips any
    directory components a client might smuggle in.
    """
    if not filename:
        return None
    name = PurePath(filename).name.strip()
    return name[:500] or None


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def create_image(
    db: Session, page_id: uuid.UUID, data: ImagePageObjectCreate, user: User
):
    page, _ = page_object_service.load_page_authorized(
        db, page_id, user, require_draft=True
    )
    # Reserved bands: nothing is placed in them.
    page_object_service.assert_outside_bands(data, page)

    obj = PageObject(
        form_variant_page_id=page.id,
        object_type=PageObjectType.IMAGE,
        x=data.x, y=data.y, width=data.width, height=data.height,
        z_index=data.z_index, label=data.label, description=data.description,
        visible=data.visible, locked=data.locked,
        # "Show on all pages": a fresh linked group, this object its
        # template. Existing pages aren't copied to; new pages get a linked copy.
        shared_group_id=uuid.uuid4() if data.shared_across_pages else None,
        shared_across_pages=data.shared_across_pages,
    )
    page_object_repo.add(db, obj)

    image_object_repo.add(
        db,
        ImageObjectConfig(page_object_id=obj.id, fit=data.config.fit),
    )

    audit_service.log_event(
        db, actor_user_id=user.id, action=audit_service.PAGE_OBJECT_CREATED,
        entity_type="PageObject", entity_id=obj.id, metadata={"object_type": "IMAGE"},
    )
    db.commit()
    db.refresh(obj)
    return page_object_service.build_typed_read(obj)


# ---------------------------------------------------------------------------
# Upload (multipart) — the authoritative security boundary
# ---------------------------------------------------------------------------


def upload_image(
    db: Session,
    object_id: uuid.UUID,
    data: bytes,
    declared_filename: str | None,
    user: User,
):
    """Validate, (sanitise,) store the uploaded file and populate the config."""
    obj, _ = page_object_service.load_object_authorized(
        db, object_id, user, require_draft=True
    )
    page_object_service.assert_object_type(obj, PageObjectType.IMAGE)
    config = obj.image_config
    if config is None:  # defensive — a well-formed IMAGE object always has one
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Image config not found"
        )

    # 1) Size (413). The endpoint reads at most max+1 bytes, so an oversize file
    #    lands here as max+1 and is rejected without buffering the whole upload.
    if len(data) > settings.max_image_upload_bytes:
        limit_mb = settings.max_image_upload_bytes / (1024 * 1024)
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Image is too large (max {limit_mb:.0f} MB).",
        )
    if not data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Uploaded file is empty.",
        )

    # 2) Real type from content (415 for anything outside the whitelist).
    try:
        sniffed = image_validation.sniff_image(data)
    except image_validation.UnsupportedImageError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        ) from exc

    # 3) SVG is sanitised before it is ever written (422 if unparseable/unsafe).
    if sniffed.is_svg:
        try:
            payload = svg_sanitizer.sanitize_svg(data)
        except svg_sanitizer.SvgSanitizeError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid SVG: {exc}",
            ) from exc
    else:
        payload = data

    # 4) Store under a server-generated name; replace any previous file (a
    #    re-upload may change the extension, so delete the old reference first).
    storage_service.delete_object_image(config.file_path)
    path = storage_service.save_object_image(payload, obj.id, sniffed.ext)

    config.file_path = str(path)
    config.original_filename = _display_filename(declared_filename)
    config.mime_type = sniffed.mime_type
    config.width_px = sniffed.width_px
    config.height_px = sniffed.height_px
    db.flush()

    audit_service.log_event(
        db, actor_user_id=user.id, action=audit_service.PAGE_OBJECT_CONFIG_UPDATED,
        entity_type="ImageObjectConfig", entity_id=config.id,
        metadata={"action": "image_upload", "mime_type": sniffed.mime_type},
    )
    db.commit()
    db.refresh(obj)
    return page_object_service.build_typed_read(obj)


# ---------------------------------------------------------------------------
# Read the binary (for the viewer / export-via-HTTP)
# ---------------------------------------------------------------------------


def get_image_file(
    db: Session, object_id: uuid.UUID, user: User
) -> tuple[bytes, str, str | None]:
    """Return (content, mime_type, original_filename) for an IMAGE object.

    Read-only: any status is allowed (no DRAFT gate). 404 if no file uploaded.
    """
    obj, _ = page_object_service.load_object_authorized(db, object_id, user)
    page_object_service.assert_object_type(obj, PageObjectType.IMAGE)
    config = obj.image_config
    if config is None or not config.file_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No image has been uploaded for this object.",
        )
    try:
        content = storage_service.read_object_image(config.file_path)
    except (ValueError, OSError) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Image file not found."
        ) from exc
    mime = config.mime_type or "application/octet-stream"
    return content, mime, config.original_filename


# ---------------------------------------------------------------------------
# Update config (fit) — single-Save draft model
# ---------------------------------------------------------------------------


def update_image_config(
    db: Session, object_id: uuid.UUID, data: ImageObjectConfigUpdate, user: User
):
    obj, _ = page_object_service.load_object_authorized(
        db, object_id, user, require_draft=True
    )
    page_object_service.assert_object_type(obj, PageObjectType.IMAGE)
    config = obj.image_config
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Image config not found"
        )

    changes = data.model_dump(exclude_unset=True)
    if changes:
        for field, value in changes.items():
            setattr(config, field, value)
        db.flush()
        audit_service.log_event(
            db, actor_user_id=user.id,
            action=audit_service.PAGE_OBJECT_CONFIG_UPDATED,
            entity_type="ImageObjectConfig", entity_id=config.id, metadata=changes,
        )
        db.commit()
    db.refresh(obj)
    return page_object_service.build_typed_read(obj)
