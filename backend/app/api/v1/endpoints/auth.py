# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Authentication endpoints."""
from __future__ import annotations

import re

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Response,
    UploadFile,
    status,
)
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import get_db, require_user
from app.models.user import User
from app.schemas.auth import LoginRequest, PasswordChangeRequest, TokenResponse
from app.schemas.user import UserProfileUpdate, UserRead
from app.services import audit_service, auth_service

router = APIRouter()


def _content_disposition_filename(name: str) -> str:
    """ASCII-safe filename for a Content-Disposition header (no header injection).

    Same helper used by the IMAGE object image-serve endpoint.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")
    return safe or "avatar"


def serve_avatar(
    content: bytes, mime: str, filename: str | None, fallback_id: object
) -> Response:
    """Build the avatar Response with the hardened image-serve headers.

    ``nosniff`` + a locked-down CSP with ``sandbox`` (so a browser cannot execute
    an SVG even if one slipped past the sanitizer); served ``inline``. Shared by
    the self route below AND the per-user route (``GET /users/{id}/avatar``)
    so the security headers stay byte-for-byte identical.
    """
    disp = _content_disposition_filename(filename or f"avatar-{fallback_id}")
    return Response(
        content=content,
        media_type=mime,
        headers={
            "Content-Disposition": f'inline; filename="{disp}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
            "Cache-Control": "private, max-age=0, must-revalidate",
        },
    )


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """Authenticate with email + password.  Returns a JWT access token.

    Disabled accounts are rejected with 403.
    Invalid credentials return 401 (no detail to avoid user enumeration).
    """
    token = auth_service.login(db, payload.email, payload.password)
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return TokenResponse(access_token=token)


@router.get("/me", response_model=UserRead)
def me(current_user: User = Depends(require_user)) -> User:
    """Return the currently authenticated user's profile."""
    return current_user


@router.patch("/me", response_model=UserRead)
def update_me(
    payload: UserProfileUpdate,
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> User:
    """Self-service edit of the caller's own identity.

    Only ``first_name``/``last_name`` are editable; ``role``/``disabled``/``email``
    are unreachable by construction (see :class:`UserProfileUpdate`). The
    ``full_name`` column is left untouched (it is no longer populated or derived).

    An empty body is a no-op that returns the unchanged profile.
    """
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        return current_user

    for field, value in changes.items():
        setattr(current_user, field, value)

    audit_service.log_event(
        db,
        actor_user_id=current_user.id,
        action="user.profile_updated",
        entity_type="User",
        entity_id=current_user.id,
        metadata={"fields": sorted(changes.keys())},
    )
    db.commit()
    db.refresh(current_user)
    return current_user


# ---------------------------------------------------------------------------
# Profile avatar — current user only.
# ---------------------------------------------------------------------------


@router.post("/me/avatar", response_model=UserRead)
async def upload_my_avatar(
    file: UploadFile = File(...),
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> User:
    """Upload (or replace) the caller's profile photo.

    Validation is authoritative in the service: real MIME sniffed from content,
    size cap, SVG sanitization, server-generated filename. Reads at most max+1
    bytes so an oversize upload is rejected (413) without buffering the whole
    file (mirrors the IMAGE object image upload).
    """
    data = await file.read(settings.max_image_upload_bytes + 1)
    return auth_service.upload_avatar(db, current_user, data, file.filename)


@router.get("/me/avatar")
def get_my_avatar(
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Serve the caller's avatar with the SAME security headers as the IMAGE
    object image serve: ``nosniff`` + a locked-down CSP with ``sandbox`` (so a
    browser cannot execute an SVG even if one slipped past the sanitizer);
    served ``inline``.
    404 when no avatar.
    """
    content, mime, filename = auth_service.get_avatar_file(db, current_user)
    return serve_avatar(content, mime, filename, current_user.id)


@router.delete("/me/avatar", response_model=UserRead)
def delete_my_avatar(
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> User:
    """Remove the caller's avatar (idempotent). Returns the refreshed profile so
    the FE sees ``has_avatar=false`` and falls back to initials."""
    return auth_service.delete_avatar(db, current_user)


@router.post("/change-password", response_model=UserRead)
def change_password(
    payload: PasswordChangeRequest,
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> User:
    """Change the authenticated user's password and clear must_change_password.

    Backs both the forced first-login change and self-service changes:

    * Forced first-login (must_change_password): no ``current_password`` needed.
    * Self-service: ``current_password`` is required and verified.

    Returns the updated profile so the client sees ``must_change_password=False``
    immediately.
    """
    return auth_service.change_password(
        db, current_user, payload.new_password, payload.current_password
    )
