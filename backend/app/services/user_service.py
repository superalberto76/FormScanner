# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""User management service — admin-driven user creation, listing + actions.

Covers admin-driven creation of a non-admin USER, the read-only admin listing
(paginated + searchable) and the per-row admin actions: disable/enable and
temporary-password reset. ALL actions target USERS only and reject ADMIN
targets (409).

Password rules are kept deliberately SEPARATE: the admin-assigned TEMPORARY
password (create + reset here) uses min length ONLY (``PASSWORD_MIN_LENGTH``); the
RICH policy lives in ``auth_service.change_password`` and applies when the user
changes the temp password at next login. These two paths never merge.
"""
from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import PASSWORD_MIN_LENGTH, hash_password
from app.models.enums import UserRole
from app.models.user import User
from app.repositories import form_project_member_repo, user_repo
from app.schemas.user import (
    PaginatedUsers,
    UserCreateByAdmin,
    UserListItem,
    UserSearchItem,
)
from app.services import audit_service, auth_service, form_project_service

# Minimum query length: short/empty queries return [] so the endpoint can't be
# used to enumerate the whole user table.
_SEARCH_MIN_CHARS = 2


def create_user_by_admin(
    db: Session, data: UserCreateByAdmin, actor: User
) -> User:
    """Create a non-admin user on behalf of *actor* (an admin).

    Security invariants enforced here, regardless of payload:
      * ``role`` is always USER — this endpoint never mints admins.
      * ``must_change_password`` is always True — the assigned password is
        provisional and must be changed at first login.
      * the password is stored only as a bcrypt hash.

    A duplicate email raises HTTP 409 ("Email already in use") rather than
    surfacing a 500 from the unique-constraint violation.
    """
    # Fast path: a clean 409 before we attempt the insert. The unique index is
    # still the source of truth (see the IntegrityError guard below) for the
    # race where two requests pass this check concurrently.
    if user_repo.get_by_email(db, data.email) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email already in use"
        )

    user = user_repo.create_user(
        db,
        email=data.email,
        password_hash=hash_password(data.password),
        first_name=data.first_name,
        last_name=data.last_name,
        role=UserRole.USER,
        must_change_password=True,
    )

    audit_service.log_user_created(
        db, actor.id, user.id, {"email": user.email, "role": user.role.value}
    )

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email already in use"
        )
    db.refresh(user)
    return user


def list_users(
    db: Session,
    *,
    q: str | None,
    limit: int,
    offset: int,
    actor: User,
) -> PaginatedUsers:
    """Return one page of the full user list for the admin screen.

    Admin-only. The endpoint already gates on ``require_admin``; re-asserting the
    role here keeps the authorization at the service boundary too, so the
    invariant holds no matter who calls the service (defense-in-depth, mirroring
    how ``search_users`` gates via ``require_any_project_owner``).

    Unlike ``search_users`` (the collaborator picker) this lists EVERYONE — admins
    and disabled users included. Read-only: no disable/reset here.
    """
    if actor.role is not UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )

    users, total = user_repo.search_paginated(db, q=q, limit=limit, offset=offset)
    return PaginatedUsers(
        items=[UserListItem.model_validate(u) for u in users],
        total=total,
        limit=limit,
        offset=offset,
    )


def _require_admin_actor(actor: User) -> None:
    """403 unless *actor* is an admin (service-boundary gate; mirrors list_users)."""
    if actor.role is not UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )


def _load_target_user(db: Session, user_id: uuid.UUID) -> User:
    """Load the action target or 404."""
    target = user_repo.get_by_id(db, user_id)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )
    return target


def set_user_disabled(
    db: Session, user_id: uuid.UUID, disabled: bool, actor: User
) -> User:
    """Enable/disable a USER account (admin-only).

    Disabling blocks login (``get_current_user`` already 403s a disabled user);
    enabling restores it. ADMIN targets are rejected with 409 — which also forbids
    self, since the actor is necessarily an admin. Access-only: this never touches
    the password or project memberships.
    """
    _require_admin_actor(actor)
    target = _load_target_user(db, user_id)

    if target.role is UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Admin accounts cannot be disabled.",
        )

    target.disabled = disabled
    audit_service.log_event(
        db,
        actor_user_id=actor.id,
        action=audit_service.USER_DISABLED if disabled else audit_service.USER_ENABLED,
        entity_type="User",
        entity_id=target.id,
    )
    db.commit()
    db.refresh(target)
    return target


def reset_user_password(
    db: Session, user_id: uuid.UUID, new_password: str, actor: User
) -> User:
    """Set a TEMPORARY password for a USER and force a change at next login.

    Admin-only. ADMIN targets are rejected with 409. The new password is
    validated with the min-length rule ONLY — NOT the rich policy and NOT via
    ``auth_service.change_password``; the rich policy is enforced when the user
    changes this temp password at next login (``must_change_password=True``).
    The plaintext password is NEVER logged. Does not touch ``disabled``.
    """
    _require_admin_actor(actor)
    target = _load_target_user(db, user_id)

    if target.role is UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Admin passwords cannot be reset here.",
        )

    # Temporary-password rule: min length ONLY (no rich policy). Reuse the policy
    # module's message style so the 422 reads consistently with the rich path.
    if len(new_password) < PASSWORD_MIN_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Password must be at least {PASSWORD_MIN_LENGTH} characters long"
            ),
        )

    target.password_hash = hash_password(new_password)
    target.must_change_password = True
    # NEVER log the password (not even its length) — entity_id is enough to audit.
    audit_service.log_event(
        db,
        actor_user_id=actor.id,
        action=audit_service.USER_PASSWORD_RESET,
        entity_type="User",
        entity_id=target.id,
    )
    db.commit()
    db.refresh(target)
    return target


def search_users(
    db: Session,
    q: str,
    exclude_project_id: uuid.UUID | None,
    actor: User,
) -> list[UserSearchItem]:
    """Find existing users to add as collaborators (owner-or-admin only).

    Gated by ``require_any_project_owner``. Queries shorter than
    ``_SEARCH_MIN_CHARS`` (after trimming) return ``[]`` so the endpoint cannot
    enumerate users. When *exclude_project_id* is given, users already members of
    that project are filtered out. Disabled users are never returned.
    """
    form_project_service.require_any_project_owner(db, actor)

    q = q.strip()
    if len(q) < _SEARCH_MIN_CHARS:
        return []

    exclude_ids: set[uuid.UUID] = set()
    if exclude_project_id is not None:
        exclude_ids = form_project_member_repo.member_user_ids(
            db, exclude_project_id
        )

    users = user_repo.search(db, q, exclude_ids=exclude_ids)
    return [UserSearchItem.model_validate(u) for u in users]


def get_user_avatar(
    db: Session, user_id: uuid.UUID
) -> tuple[bytes, str, str | None]:
    """Return (content, mime, original_filename) for *user_id*'s avatar.

    Avatars are public to authenticated users — no
    membership check. 404 if the user does not exist; the shared
    ``auth_service.get_avatar_file`` 404s if no avatar was uploaded. Reuses the
    self-serve logic verbatim (keyed by the resolved user) so behaviour matches
    ``GET /auth/me/avatar``.
    """
    user = user_repo.get_by_id(db, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )
    return auth_service.get_avatar_file(db, user)
