# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Data-access layer for the User model."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.enums import UserRole


def get_by_email(db: Session, email: str) -> User | None:
    return db.scalars(select(User).where(User.email == email)).first()


def get_by_id(db: Session, user_id: uuid.UUID) -> User | None:
    return db.get(User, user_id)


def list_by_ids(db: Session, user_ids: set[uuid.UUID]) -> dict[uuid.UUID, User]:
    """{user_id: User} for *user_ids*, in ONE query; unknown ids are absent.

    No query at all for an empty set, so a caller can pass whatever it collected
    without guarding it (e.g. the holders of a batch's active locks).
    """
    if not user_ids:
        return {}
    stmt = select(User).where(User.id.in_(user_ids))
    return {u.id: u for u in db.scalars(stmt).all()}


def search(
    db: Session,
    q: str,
    *,
    limit: int = 20,
    exclude_ids: set[uuid.UUID] | None = None,
) -> list[User]:
    """Case-insensitive search over first/last name and email.

    Matches users whose ``first_name``, ``last_name`` or ``email`` CONTAINS *q*
    (``ILIKE %q%``). Disabled users and any id in *exclude_ids* are excluded.
    Ordered by last/first/email, capped at *limit*.
    """
    pattern = f"%{q}%"
    stmt = (
        select(User)
        .where(
            User.disabled.is_(False),
            or_(
                User.first_name.ilike(pattern),
                User.last_name.ilike(pattern),
                User.email.ilike(pattern),
            ),
        )
        .order_by(User.last_name, User.first_name, User.email)
        .limit(limit)
    )
    if exclude_ids:
        stmt = stmt.where(User.id.notin_(exclude_ids))
    return list(db.scalars(stmt).all())


def search_paginated(
    db: Session,
    *,
    q: str | None,
    limit: int,
    offset: int,
) -> tuple[list[User], int]:
    """Paginated user listing for the admin management screen.

    Unlike :func:`search` (the collaborator picker), this lists EVERYONE — admins
    AND disabled users are included; deciding which rows get actions is the
    caller's job, not the repo's. When *q* has at least one non-space char, rows are
    filtered to those whose ``first_name``, ``last_name`` or ``email`` CONTAINS
    *q* (``ILIKE %q%``); a blank/None *q* matches all users. Ordered by
    last/first/email for a stable page across requests.

    Returns ``(page, total)`` where *page* is the requested ``[offset, offset +
    limit)`` slice and *total* is the full count of matching rows (so the FE can
    render "X–Y of N" and enable/disable the pager).
    """
    base = select(User)
    if q is not None and q.strip():
        pattern = f"%{q.strip()}%"
        base = base.where(
            or_(
                User.first_name.ilike(pattern),
                User.last_name.ilike(pattern),
                User.email.ilike(pattern),
            )
        )

    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0

    page_stmt = (
        base.order_by(User.last_name, User.first_name, User.email)
        .limit(limit)
        .offset(offset)
    )
    users = list(db.scalars(page_stmt).all())
    return users, total


def set_last_login(db: Session, user: User) -> None:
    user.last_login_at = datetime.now(timezone.utc)
    db.flush()


def create_admin_user(db: Session, *, email: str, password_hash: str) -> User:
    """Insert a new ADMIN user and flush (caller must commit)."""
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=password_hash,
        role=UserRole.ADMIN,
        disabled=False,
    )
    db.add(user)
    db.flush()
    return user


def create_user(
    db: Session,
    *,
    email: str,
    password_hash: str,
    first_name: str | None = None,
    last_name: str | None = None,
    role: UserRole = UserRole.USER,
    must_change_password: bool = False,
) -> User:
    """Insert a new user and flush (caller must commit)."""
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=password_hash,
        first_name=first_name,
        last_name=last_name,
        role=role,
        disabled=False,
        must_change_password=must_change_password,
    )
    db.add(user)
    db.flush()
    return user
