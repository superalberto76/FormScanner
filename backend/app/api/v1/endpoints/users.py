# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""User management endpoints.

Mounted at /users. User creation is admin-only; the user-search used to add
project collaborators is open to any project OWNER or admin (the
service enforces that gate).
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.api.v1.endpoints.auth import serve_avatar
from app.core.deps import get_db, require_admin, require_user
from app.models.user import User
from app.schemas.user import (
    AdminPasswordReset,
    PaginatedUsers,
    UserCreateByAdmin,
    UserDisableUpdate,
    UserRead,
    UserSearchItem,
)
from app.services import user_service

router = APIRouter()


@router.post("", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def create_user(
    data: UserCreateByAdmin,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> User:
    """Create a non-admin user (role=USER, must_change_password=True).

    Admin-only. A duplicate email returns 409 ("Email already in use").
    """
    return user_service.create_user_by_admin(db, data, admin)


@router.get("", response_model=PaginatedUsers)
def list_users(
    q: str | None = Query(
        None, description="Filter by first name, last name or email (ILIKE)"
    ),
    limit: int = Query(20, ge=1, le=100, description="Page size (1..100)"),
    offset: int = Query(0, ge=0, description="Rows to skip before the page"),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> PaginatedUsers:
    """List users for the admin "User management" screen.

    Admin-only. Paginated and searchable by first name, last name or email
    (case-insensitive substring). Lists EVERYONE — admins and disabled users
    included. Read-only: disabling and password resets are separate endpoints.
    """
    return user_service.list_users(
        db, q=q, limit=limit, offset=offset, actor=admin
    )


@router.patch("/{user_id}", response_model=UserRead)
def set_user_disabled(
    user_id: uuid.UUID,
    data: UserDisableUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> User:
    """Enable/disable a USER account. Admin-only.

    Minimal by design: this endpoint only flips ``disabled``. Disabling an ADMIN
    (including self) is rejected with 409. Returns the refreshed user.
    """
    return user_service.set_user_disabled(db, user_id, data.disabled, admin)


@router.post("/{user_id}/reset-password", response_model=UserRead)
def reset_user_password(
    user_id: uuid.UUID,
    data: AdminPasswordReset,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> User:
    """Set a TEMPORARY password for a USER and force a change at next login.

    Admin-only. Min-length rule only (no rich policy); ADMIN targets are
    rejected with 409. Returns the refreshed user, so the FE can see
    ``must_change_password=True``.
    """
    return user_service.reset_user_password(db, user_id, data.new_password, admin)


@router.get("/search", response_model=list[UserSearchItem])
def search_users(
    q: str = Query("", description="Name/email fragment (min 2 chars)"),
    exclude_project_id: uuid.UUID | None = Query(
        None, description="Exclude users already members of this project"
    ),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Search existing users by name/email (any project OWNER or admin).

    Queries shorter than 2 chars return an empty list (no enumeration).
    """
    return user_service.search_users(db, q, exclude_project_id, user)


@router.get("/{user_id}/avatar")
def get_user_avatar(
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
) -> Response:
    """Serve any user's avatar.

    Avatars are PUBLIC to authenticated users — there is NO owner/membership
    check, only authentication. 404 if the user does not exist or has no avatar.
    Reuses ``serve_avatar`` so the security headers (nosniff + locked-down CSP +
    Content-Disposition) are byte-for-byte identical to ``GET /auth/me/avatar``.
    """
    content, mime, filename = user_service.get_user_avatar(db, user_id)
    return serve_avatar(content, mime, filename, user_id)
