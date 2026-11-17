# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Authentication business logic."""
from __future__ import annotations

from pathlib import PurePath

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import (
    create_access_token,
    hash_password,
    password_policy_errors,
    verify_password,
)
from app.models.user import User
from app.repositories import user_repo
from app.services import audit_service, storage_service
from app.utils import image_validation, svg_sanitizer


def authenticate(db: Session, email: str, password: str) -> User | None:
    """Return the User if credentials are valid and the account is active.

    Returns None if:
    - no user with that email exists
    - password does not match
    - account is disabled
    """
    user = user_repo.get_by_email(db, email)
    if user is None:
        return None
    if not verify_password(password, user.password_hash):
        return None
    if user.disabled:
        return None
    return user


def login(db: Session, email: str, password: str) -> str | None:
    """Authenticate and, on success, record last_login and return a JWT.

    Returns None on any failure (bad credentials or disabled).
    """
    user = authenticate(db, email, password)
    if user is None:
        return None
    user_repo.set_last_login(db, user)
    db.commit()
    return create_access_token(user.id, user.role)


def change_password(
    db: Session,
    user: User,
    new_password: str,
    current_password: str | None = None,
) -> User:
    """Set a new password for *user* and clear the must-change-password flag.

    Two modes, branched on the user's CURRENT must_change_password flag (read
    BEFORE any mutation):

    * Self-service (must_change_password is False): ``current_password`` is
      REQUIRED and must match the stored hash, else 400.
    * Forced first-login (must_change_password is True): ``current_password`` is
      NOT required and is ignored if supplied.

    In both modes ``new_password`` must satisfy the shared password policy
    (:func:`app.core.security.password_policy_errors`); otherwise 422.

    The caller (endpoint) is responsible for authentication; this only updates
    the already-resolved account.
    """
    if not user.must_change_password:
        if not current_password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Current password is required",
            )
        if not verify_password(current_password, user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Current password is incorrect",
            )

    errors = password_policy_errors(new_password)
    if errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="; ".join(errors),
        )

    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    db.commit()
    db.refresh(user)
    return user


# ---------------------------------------------------------------------------
# Profile avatar — self-service, current user only.
#
# Mirrors image_object_service's upload/serve/delete for IMAGE objects (same
# validation, SVG sanitization and storage discipline), but keyed by the
# authenticated user under the separate avatar_images_dir. Authorization is
# implicit: the caller is always the authenticated user — no cross-user access.
# ---------------------------------------------------------------------------


def _avatar_display_filename(filename: str | None) -> str | None:
    """Sanitised, length-capped original filename for display/download only.

    Never used to build an on-disk path — only stored for the UI. Strips any
    directory components a client might smuggle in. (Mirrors the IMAGE service.)
    """
    if not filename:
        return None
    name = PurePath(filename).name.strip()
    return name[:500] or None


def upload_avatar(
    db: Session,
    user: User,
    data: bytes,
    declared_filename: str | None,
) -> User:
    """Validate, (sanitise,) store the uploaded avatar and update the user.

    Mirrors ``image_object_service.upload_image``: size cap (413), empty (422),
    real MIME sniffed from content (415), SVG sanitised before write (422). The
    previous file (if any) is replaced. Returns the refreshed user.
    """
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
    storage_service.delete_avatar_image(user.avatar_path)
    path = storage_service.save_avatar_image(payload, user.id, sniffed.ext)

    user.avatar_path = str(path)
    user.avatar_mime = sniffed.mime_type
    user.avatar_original_filename = _avatar_display_filename(declared_filename)

    audit_service.log_event(
        db, actor_user_id=user.id, action="user.avatar_updated",
        entity_type="User", entity_id=user.id,
        metadata={"mime_type": sniffed.mime_type},
    )
    db.commit()
    db.refresh(user)
    return user


def get_avatar_file(db: Session, user: User) -> tuple[bytes, str, str | None]:
    """Return (content, mime_type, original_filename) for the user's avatar.

    404 if no avatar uploaded or the file is missing on disk.
    """
    if not user.avatar_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No avatar has been uploaded.",
        )
    try:
        content = storage_service.read_avatar_image(user.avatar_path)
    except (ValueError, OSError) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Avatar file not found."
        ) from exc
    mime = user.avatar_mime or "application/octet-stream"
    return content, mime, user.avatar_original_filename


def delete_avatar(db: Session, user: User) -> User:
    """Remove the user's avatar: clear the columns, then delete the file.

    Idempotent (no-op when there is no avatar). The file is removed AFTER the
    commit — same ordering as ``page_object_service.delete_object`` — so a failed
    commit never orphans the DB from disk.
    """
    old_path = user.avatar_path
    user.avatar_path = None
    user.avatar_mime = None
    user.avatar_original_filename = None

    if old_path is not None:
        audit_service.log_event(
            db, actor_user_id=user.id, action="user.avatar_deleted",
            entity_type="User", entity_id=user.id,
        )
    db.commit()
    db.refresh(user)
    storage_service.delete_avatar_image(old_path)
    return user
