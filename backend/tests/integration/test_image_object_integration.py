# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for the IMAGE PageObject.

Service-level (no HTTP) against real Postgres — mirrors the other integration
suites. Files are written to a throwaway ``tmp_path`` via a monkeypatched
``object_images_dir`` so the real storage tree is never touched.

Requires TEST_DATABASE_URL — see conftest.py (skipped without it).
"""
from __future__ import annotations

import uuid
from io import BytesIO

import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import select

from app.core.config import settings
from app.models.enums import PageObjectType
from app.models.image_object_config import ImageObjectConfig
from app.models.page_object import PageObject
from app.models.user import User
from app.repositories import form_variant_page_repo, page_object_repo
from app.schemas.page_object import ImageObjectConfigUpdate, ImagePageObjectCreate
from app.services import (
    form_variant_service,
    image_object_service,
    page_object_service,
)
from app.services import storage_service


@pytest.fixture(autouse=True)
def _image_storage(tmp_path, monkeypatch):
    """Redirect object-image storage to a temp dir for the whole module."""
    monkeypatch.setattr(settings, "object_images_dir", str(tmp_path / "object_images"))
    yield


def _png_bytes(color="red", size=(4, 6)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _new_image_object(db, page, user) -> uuid.UUID:
    read = image_object_service.create_image(
        db, page.id, ImagePageObjectCreate(x=60, y=60, width=20, height=20), user
    )
    return read.id


def _config(db, object_id) -> ImageObjectConfig:
    return db.scalar(
        select(ImageObjectConfig).where(ImageObjectConfig.page_object_id == object_id)
    )


# ---------------------------------------------------------------------------
# Create + upload happy path
# ---------------------------------------------------------------------------
def test_create_image_object_starts_without_file(db, page, user):
    read = image_object_service.create_image(
        db, page.id, ImagePageObjectCreate(x=61, y=62, width=30, height=40), user
    )
    assert read.object_type == PageObjectType.IMAGE
    assert read.image_config.has_image is False
    assert read.image_config.fit == "contain"


def test_upload_png_populates_config_and_file(db, page, user):
    object_id = _new_image_object(db, page, user)
    data = _png_bytes()

    read = image_object_service.upload_image(db, object_id, data, "photo.png", user)
    assert read.image_config.has_image is True
    assert read.image_config.mime_type == "image/png"
    assert read.image_config.width_px == 4 and read.image_config.height_px == 6
    assert read.image_config.original_filename == "photo.png"

    # File really exists on disk and round-trips through the serve helper.
    content, mime, filename = image_object_service.get_image_file(db, object_id, user)
    assert content == data
    assert mime == "image/png"
    assert filename == "photo.png"


def test_update_fit(db, page, user):
    object_id = _new_image_object(db, page, user)
    read = image_object_service.update_image_config(
        db, object_id, ImageObjectConfigUpdate(fit="cover"), user
    )
    assert read.image_config.fit == "cover"


# ---------------------------------------------------------------------------
# Upload validation (BE authoritative)
# ---------------------------------------------------------------------------
def test_upload_unsupported_type_rejected(db, page, user):
    object_id = _new_image_object(db, page, user)
    gif = BytesIO()
    Image.new("RGB", (3, 3), "blue").save(gif, format="GIF")
    with pytest.raises(HTTPException) as exc:
        image_object_service.upload_image(db, object_id, gif.getvalue(), "x.gif", user)
    assert exc.value.status_code == 415


def test_upload_oversize_rejected(db, page, user, monkeypatch):
    object_id = _new_image_object(db, page, user)
    monkeypatch.setattr(settings, "max_image_upload_bytes", 10)
    with pytest.raises(HTTPException) as exc:
        image_object_service.upload_image(db, object_id, _png_bytes(), "big.png", user)
    assert exc.value.status_code == 413


def test_svg_is_sanitized_on_upload(db, page, user):
    object_id = _new_image_object(db, page, user)
    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" onload="evil()">'
        b"<script>alert(1)</script><rect width='5' height='5'/></svg>"
    )
    read = image_object_service.upload_image(db, object_id, svg, "art.svg", user)
    assert read.image_config.mime_type == "image/svg+xml"

    content, mime, _ = image_object_service.get_image_file(db, object_id, user)
    lowered = content.lower()
    assert b"script" not in lowered
    assert b"onload" not in lowered
    assert b"alert" not in lowered
    assert b"<svg" in lowered  # still a valid SVG


# ---------------------------------------------------------------------------
# Delete removes config AND the on-disk file
# ---------------------------------------------------------------------------
def test_delete_removes_config_and_file(db, page, user):
    object_id = _new_image_object(db, page, user)
    image_object_service.upload_image(db, object_id, _png_bytes(), "p.png", user)
    file_path = _config(db, object_id).file_path
    assert storage_service.assert_within_object_images_dir(file_path).exists()

    page_object_service.delete_object(db, object_id, user)

    assert _config(db, object_id) is None
    assert db.get(PageObject, object_id) is None
    assert not storage_service.assert_within_object_images_dir(file_path).exists()


# ---------------------------------------------------------------------------
# Clone duplicates the file (independent variants)
# ---------------------------------------------------------------------------
def test_clone_duplicates_image_file(db, page, user):
    object_id = _new_image_object(db, page, user)
    image_object_service.upload_image(db, object_id, _png_bytes(), "p.png", user)
    src_path = _config(db, object_id).file_path
    variant_id = page.form_variant_id

    new_variant = form_variant_service.clone_variant(db, variant_id, user)

    # Locate the cloned IMAGE object + its config.
    clone_pages = form_variant_page_repo.list_for_variant(db, new_variant.id)
    clone_objs = [
        o
        for p in clone_pages
        for o in page_object_repo.list_for_page_with_configs(db, p.id)
        if o.object_type is PageObjectType.IMAGE
    ]
    assert len(clone_objs) == 1
    clone_cfg = clone_objs[0].image_config

    # Independent file: different path, but identical bytes, both present.
    assert clone_cfg.file_path is not None
    assert clone_cfg.file_path != src_path
    assert storage_service.read_object_image(clone_cfg.file_path) == _png_bytes()
    assert storage_service.assert_within_object_images_dir(src_path).exists()


# ---------------------------------------------------------------------------
# Authorization parity
# ---------------------------------------------------------------------------
def test_upload_requires_access(db, page, user):
    object_id = _new_image_object(db, page, user)
    intruder = User(
        email=f"intruder-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=user.role,
    )
    db.add(intruder)
    db.flush()
    with pytest.raises(HTTPException) as exc:
        image_object_service.upload_image(db, object_id, _png_bytes(), "p.png", intruder)
    assert exc.value.status_code in (403, 404)
