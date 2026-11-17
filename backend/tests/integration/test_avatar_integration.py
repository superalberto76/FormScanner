# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for the profile avatar.

Service-level (no HTTP) against real Postgres — mirrors
``test_image_object_integration.py``. Files are written to a throwaway
``tmp_path`` via a monkeypatched ``avatar_images_dir`` so the real storage tree
is never touched.

Requires TEST_DATABASE_URL — see conftest.py (the shared ``db``/``user``
fixtures are reused).
"""
from __future__ import annotations

from io import BytesIO

import pytest
from fastapi import HTTPException
from PIL import Image

from app.core.config import settings
from app.services import auth_service, storage_service


@pytest.fixture(autouse=True)
def _avatar_storage(tmp_path, monkeypatch):
    """Redirect avatar storage to a temp dir for the whole module."""
    monkeypatch.setattr(settings, "avatar_images_dir", str(tmp_path / "avatar_images"))
    yield


def _png_bytes(color="red", size=(4, 6)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Upload happy path + round-trip serve
# ---------------------------------------------------------------------------
def test_upload_png_sets_avatar_and_round_trips(db, user):
    assert user.has_avatar is False
    data = _png_bytes()

    updated = auth_service.upload_avatar(db, user, data, "me.png")
    assert updated.has_avatar is True
    assert updated.avatar_mime == "image/png"
    assert updated.avatar_original_filename == "me.png"

    content, mime, filename = auth_service.get_avatar_file(db, user)
    assert content == data
    assert mime == "image/png"
    assert filename == "me.png"


def test_get_avatar_404_when_none(db, user):
    with pytest.raises(HTTPException) as exc:
        auth_service.get_avatar_file(db, user)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Upload validation (BE authoritative) — same rules as the IMAGE object
# ---------------------------------------------------------------------------
def test_upload_unsupported_type_rejected(db, user):
    gif = BytesIO()
    Image.new("RGB", (3, 3), "blue").save(gif, format="GIF")
    with pytest.raises(HTTPException) as exc:
        auth_service.upload_avatar(db, user, gif.getvalue(), "x.gif")
    assert exc.value.status_code == 415


def test_upload_oversize_rejected(db, user, monkeypatch):
    monkeypatch.setattr(settings, "max_image_upload_bytes", 10)
    with pytest.raises(HTTPException) as exc:
        auth_service.upload_avatar(db, user, _png_bytes(), "big.png")
    assert exc.value.status_code == 413


def test_upload_empty_rejected(db, user):
    with pytest.raises(HTTPException) as exc:
        auth_service.upload_avatar(db, user, b"", "empty.png")
    assert exc.value.status_code == 422


def test_svg_is_sanitized_on_upload(db, user):
    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" onload="evil()">'
        b"<script>alert(1)</script><rect width='5' height='5'/></svg>"
    )
    updated = auth_service.upload_avatar(db, user, svg, "art.svg")
    assert updated.avatar_mime == "image/svg+xml"

    content, mime, _ = auth_service.get_avatar_file(db, user)
    lowered = content.lower()
    assert b"script" not in lowered
    assert b"onload" not in lowered
    assert b"alert" not in lowered
    assert b"<svg" in lowered  # still a valid SVG


# ---------------------------------------------------------------------------
# Delete clears columns AND removes the file (idempotent)
# ---------------------------------------------------------------------------
def test_delete_clears_columns_and_file(db, user):
    auth_service.upload_avatar(db, user, _png_bytes(), "p.png")
    path = user.avatar_path
    assert storage_service.assert_within_avatar_images_dir(path).exists()

    updated = auth_service.delete_avatar(db, user)

    assert updated.has_avatar is False
    assert updated.avatar_path is None
    assert updated.avatar_mime is None
    assert updated.avatar_original_filename is None
    assert not storage_service.assert_within_avatar_images_dir(path).exists()


def test_delete_is_idempotent_without_avatar(db, user):
    # No avatar yet: delete is a no-op that still returns the user.
    updated = auth_service.delete_avatar(db, user)
    assert updated.has_avatar is False


# ---------------------------------------------------------------------------
# Re-upload replaces the previous file (even across extensions)
# ---------------------------------------------------------------------------
def test_reupload_replaces_previous_file(db, user):
    auth_service.upload_avatar(db, user, _png_bytes(), "first.png")
    old_path = user.avatar_path
    assert old_path.endswith(".png")
    assert storage_service.assert_within_avatar_images_dir(old_path).exists()

    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="5" height="5"/></svg>'
    auth_service.upload_avatar(db, user, svg, "second.svg")
    new_path = user.avatar_path

    assert new_path != old_path
    assert new_path.endswith(".svg")
    assert storage_service.assert_within_avatar_images_dir(new_path).exists()
    # The previous PNG was deleted, not orphaned.
    assert not storage_service.assert_within_avatar_images_dir(old_path).exists()
