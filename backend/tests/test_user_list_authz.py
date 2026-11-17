# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure (DB-free) authorization tests for the admin user-management service.

``list_users`` / ``set_user_disabled`` / ``reset_user_password`` all authorize
BEFORE they touch the database, so the "non-admin → 403" rule is checkable
without a Postgres test DB — these run anywhere, like ``test_password_policy.py``
/ ``test_user_profile_schema.py``. The DB-backed behaviour (pagination/search,
disable/enable, reset) lives in ``tests/integration/`` (skipped without
``TEST_DATABASE_URL``).
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.models.enums import UserRole
from app.models.user import User
from app.services import user_service


def _user(role: UserRole) -> User:
    # A bare model instance — constructing it needs no session.
    return User(email="actor@example.com", password_hash="x", role=role)


# db is deliberately None throughout: a non-admin must be turned away before any
# query runs, so passing None proves the unauthorized path never touches the DB.
def test_list_users_rejects_non_admin_before_any_db_access():
    with pytest.raises(HTTPException) as exc:
        user_service.list_users(
            None, q=None, limit=20, offset=0, actor=_user(UserRole.USER)
        )
    assert exc.value.status_code == 403


def test_set_user_disabled_rejects_non_admin_before_any_db_access():
    with pytest.raises(HTTPException) as exc:
        user_service.set_user_disabled(
            None, uuid.uuid4(), True, actor=_user(UserRole.USER)
        )
    assert exc.value.status_code == 403


def test_reset_user_password_rejects_non_admin_before_any_db_access():
    with pytest.raises(HTTPException) as exc:
        user_service.reset_user_password(
            None, uuid.uuid4(), "longenough1", actor=_user(UserRole.USER)
        )
    assert exc.value.status_code == 403
