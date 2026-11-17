# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""FastAPI dependency injection helpers."""
from __future__ import annotations

import uuid
from typing import Generator

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy.orm import Session

from app.core.security import decode_access_token
from app.db.session import SessionLocal
from app.models.enums import UserRole
from app.models.user import User
from app.repositories import user_repo

_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


# ---------------------------------------------------------------------------
# Database session
# ---------------------------------------------------------------------------

def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Token → User
# ---------------------------------------------------------------------------

def get_current_user(
    token: str = Depends(_oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Decode JWT, resolve the user, and enforce the disabled flag."""
    credentials_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_access_token(token)
        sub: str | None = payload.get("sub")
        if sub is None:
            raise credentials_exc
        user_id = uuid.UUID(sub)
    except (JWTError, ValueError):
        raise credentials_exc

    user = user_repo.get_by_id(db, user_id)
    if user is None:
        raise credentials_exc
    if user.disabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is disabled",
        )
    return user


# ---------------------------------------------------------------------------
# RBAC helpers
# ---------------------------------------------------------------------------

def require_user(current_user: User = Depends(get_current_user)) -> User:
    """Require any authenticated, non-disabled user."""
    return current_user


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """Require an authenticated user with role=ADMIN."""
    if current_user.role is not UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    return current_user


# ---------------------------------------------------------------------------
# Authorization primitives (reusable from services, not just routers)
# ---------------------------------------------------------------------------

def assert_owner(resource_owner_id: uuid.UUID, current_user: User) -> None:
    """Raise 403 if *current_user* is not the owner of a resource.

    Admins bypass the check.
    """
    if current_user.role is UserRole.ADMIN:
        return
    if resource_owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not own this resource",
        )
