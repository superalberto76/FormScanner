# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for admin user creation + forced password change.

Covers the service layer (create_user_by_admin, change_password) plus
the require_admin authorization gate. Requires TEST_DATABASE_URL — see
conftest.py (the ``db``/``user`` fixtures are shared).
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.core import deps
from app.core.security import verify_password
from app.models.enums import UserRole
from app.models.user import User
from app.schemas.user import UserCreateByAdmin
from app.services import auth_service, user_service


@pytest.fixture
def admin(db) -> User:
    a = User(
        email=f"admin-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.ADMIN,
    )
    db.add(a)
    db.flush()
    return a


def _payload(email: str | None = None) -> UserCreateByAdmin:
    return UserCreateByAdmin(
        first_name="Mario",
        last_name="Rossi",
        email=email or f"new-{uuid.uuid4().hex}@example.com",
        password="initial-pw-123",
    )


# ---------------------------------------------------------------------------
# create_user_by_admin
# ---------------------------------------------------------------------------
def test_admin_creates_user_is_non_admin_and_must_change_password(db, admin):
    data = _payload()
    created = user_service.create_user_by_admin(db, data, admin)

    assert created.role is UserRole.USER
    assert created.must_change_password is True
    assert created.first_name == "Mario"
    assert created.last_name == "Rossi"
    assert created.disabled is False
    # Password is stored hashed, never in plaintext, and verifies.
    assert created.password_hash != data.password
    assert verify_password(data.password, created.password_hash) is True


def test_duplicate_email_raises_409(db, admin):
    email = f"dup-{uuid.uuid4().hex}@example.com"
    user_service.create_user_by_admin(db, _payload(email), admin)

    with pytest.raises(HTTPException) as exc:
        user_service.create_user_by_admin(db, _payload(email), admin)
    assert exc.value.status_code == 409
    assert "already in use" in exc.value.detail.lower()


# ---------------------------------------------------------------------------
# require_admin gate
# ---------------------------------------------------------------------------
def test_require_admin_allows_admin(admin):
    assert deps.require_admin(current_user=admin) is admin


def test_require_admin_rejects_non_admin(user):
    with pytest.raises(HTTPException) as exc:
        deps.require_admin(current_user=user)
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# change_password clears the flag
# ---------------------------------------------------------------------------
def test_change_password_clears_flag_and_updates_hash(db, admin):
    created = user_service.create_user_by_admin(db, _payload(), admin)
    assert created.must_change_password is True
    old_hash = created.password_hash

    # Forced first-login needs no current_password, but the new password must
    # satisfy the shared policy: upper/lower/digit/special, >=8.
    updated = auth_service.change_password(db, created, "Brand-New-Pw-456")

    assert updated.must_change_password is False
    assert updated.password_hash != old_hash
    assert verify_password("Brand-New-Pw-456", updated.password_hash) is True
