# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for the change-password flows.

Exercises the service layer (``auth_service.change_password``) directly, matching
the style of ``test_user_admin_integration.py``. Requires TEST_DATABASE_URL —
see conftest.py (the shared ``db``/``user`` fixtures).

Covers:
* Forced first-login (must_change_password=True) still succeeds WITHOUT a
  current password and clears the flag.
* Self-service (must_change_password=False): missing current password -> 400;
  wrong current password -> 400; valid current + compliant new -> 200.
* A policy-violating new password -> 422 in both modes.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.core.security import hash_password, verify_password
from app.services import auth_service

# A new password that satisfies the full policy (upper/lower/digit/special, >=8).
GOOD_NEW = "NewPass1@"
# Self-service users start from this (already policy-compliant) password.
OLD = "OldPass1@"


# ---------------------------------------------------------------------------
# Forced first-login (must_change_password=True)
# ---------------------------------------------------------------------------
def test_forced_change_succeeds_without_current_password(db, user):
    user.must_change_password = True
    user.password_hash = hash_password("temp-assigned")
    db.flush()

    updated = auth_service.change_password(db, user, GOOD_NEW)

    assert updated.must_change_password is False
    assert verify_password(GOOD_NEW, updated.password_hash) is True


def test_forced_change_ignores_current_password_if_sent(db, user):
    user.must_change_password = True
    user.password_hash = hash_password("temp-assigned")
    db.flush()

    # A wrong/irrelevant current_password must NOT block the forced flow.
    updated = auth_service.change_password(db, user, GOOD_NEW, "anything")

    assert updated.must_change_password is False
    assert verify_password(GOOD_NEW, updated.password_hash) is True


# ---------------------------------------------------------------------------
# Self-service (must_change_password=False)
# ---------------------------------------------------------------------------
def _self_service_user(db, user):
    user.must_change_password = False
    user.password_hash = hash_password(OLD)
    db.flush()
    return user


def test_self_service_missing_current_password_is_400(db, user):
    _self_service_user(db, user)
    with pytest.raises(HTTPException) as exc:
        auth_service.change_password(db, user, GOOD_NEW)
    assert exc.value.status_code == 400
    assert "required" in exc.value.detail.lower()


def test_self_service_wrong_current_password_is_400(db, user):
    _self_service_user(db, user)
    with pytest.raises(HTTPException) as exc:
        auth_service.change_password(db, user, GOOD_NEW, "WrongPass1@")
    assert exc.value.status_code == 400
    assert "incorrect" in exc.value.detail.lower()


def test_self_service_valid_current_and_compliant_new_succeeds(db, user):
    _self_service_user(db, user)

    updated = auth_service.change_password(db, user, GOOD_NEW, OLD)

    assert updated.must_change_password is False
    assert verify_password(GOOD_NEW, updated.password_hash) is True
    # Old password no longer works.
    assert verify_password(OLD, updated.password_hash) is False


# ---------------------------------------------------------------------------
# Policy enforcement (both modes)
# ---------------------------------------------------------------------------
def test_policy_violation_is_422_forced(db, user):
    user.must_change_password = True
    user.password_hash = hash_password("temp-assigned")
    db.flush()
    with pytest.raises(HTTPException) as exc:
        auth_service.change_password(db, user, "short")  # fails policy
    assert exc.value.status_code == 422


def test_policy_violation_is_422_self_service(db, user):
    _self_service_user(db, user)
    with pytest.raises(HTTPException) as exc:
        # Correct current password, but new one violates the policy.
        auth_service.change_password(db, user, "alllowercase", OLD)
    assert exc.value.status_code == 422
