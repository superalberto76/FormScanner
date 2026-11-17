# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for the admin user ACTIONS.

Covers ``user_service.set_user_disabled`` and ``reset_user_password``:
disable→login blocked / enable→restored, ADMIN targets rejected (409),
memberships untouched, the temp-password reset (min-8, forces change, leaves
``disabled`` alone, never logs the password), and the proof that the RICH policy
still guards the user's next-login change (the unchanged
``auth_service.change_password`` path).

Requires ``TEST_DATABASE_URL`` — the ``db``/``user`` fixtures come from conftest.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.core import deps
from app.core.security import create_access_token, verify_password
from app.models.enums import FormProjectType, ProjectRole, UserRole
from app.models.form_project import FormProject
from app.models.form_project_member import FormProjectMember
from app.models.user import User
from app.repositories import audit_repo, form_project_member_repo
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


def _make_admin(db) -> User:
    other = User(
        email=f"admin2-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.ADMIN,
    )
    db.add(other)
    db.flush()
    return other


# ---------------------------------------------------------------------------
# set_user_disabled
# ---------------------------------------------------------------------------
def test_disable_blocks_login_and_enable_restores_it(db, admin, user):
    # Disable → the existing get_current_user gate 403s the (otherwise valid) token.
    updated = user_service.set_user_disabled(db, user.id, True, admin)
    assert updated.disabled is True

    token = create_access_token(user.id, user.role)
    with pytest.raises(HTTPException) as exc:
        deps.get_current_user(token=token, db=db)
    assert exc.value.status_code == 403  # "Account is disabled"

    # Enable → access restored with the same token.
    restored = user_service.set_user_disabled(db, user.id, False, admin)
    assert restored.disabled is False
    resolved = deps.get_current_user(token=token, db=db)
    assert resolved.id == user.id


def test_cannot_disable_an_admin_target(db, admin):
    other_admin = _make_admin(db)
    with pytest.raises(HTTPException) as exc:
        user_service.set_user_disabled(db, other_admin.id, True, admin)
    assert exc.value.status_code == 409


def test_cannot_disable_self(db, admin):
    # The actor is an admin, so targeting self hits the same ADMIN guard (409).
    with pytest.raises(HTTPException) as exc:
        user_service.set_user_disabled(db, admin.id, True, admin)
    assert exc.value.status_code == 409


def test_missing_target_is_404(db, admin):
    with pytest.raises(HTTPException) as exc:
        user_service.set_user_disabled(db, uuid.uuid4(), True, admin)
    assert exc.value.status_code == 404


def test_disable_does_not_touch_memberships(db, admin, user):
    project = FormProject(owner_id=admin.id, type=FormProjectType.EXAM, name="P")
    db.add(project)
    db.flush()
    member = FormProjectMember(
        project_id=project.id, user_id=user.id, role=ProjectRole.COLLABORATOR
    )
    db.add(member)
    db.flush()

    user_service.set_user_disabled(db, user.id, True, admin)

    # The membership row survives the disable (access block only).
    assert form_project_member_repo.get(db, project.id, user.id) is not None


# ---------------------------------------------------------------------------
# reset_user_password
# ---------------------------------------------------------------------------
def test_reset_sets_temp_password_and_forces_change(db, admin, user):
    user.disabled = False
    old_hash = user.password_hash

    updated = user_service.reset_user_password(db, user.id, "temp-pass-123", admin)

    assert updated.password_hash != old_hash
    assert verify_password("temp-pass-123", updated.password_hash) is True
    assert updated.must_change_password is True
    assert updated.disabled is False  # reset never changes the disabled flag


def test_reset_too_short_is_422_and_leaves_password_unchanged(db, admin, user):
    old_hash = user.password_hash
    with pytest.raises(HTTPException) as exc:
        user_service.reset_user_password(db, user.id, "short", admin)
    assert exc.value.status_code == 422
    assert "at least" in exc.value.detail.lower()
    # The failed reset did not mutate the stored hash.
    assert user.password_hash == old_hash


def test_cannot_reset_an_admin_password(db, admin):
    other_admin = _make_admin(db)
    with pytest.raises(HTTPException) as exc:
        user_service.reset_user_password(db, other_admin.id, "temp-pass-123", admin)
    assert exc.value.status_code == 409


def test_reset_never_logs_the_password(db, admin, user):
    secret = "sup3r-secret-temp"
    user_service.reset_user_password(db, user.id, secret, admin)

    entries = audit_repo.query(
        db, entity_type="User", entity_id=str(user.id), action="user.password_reset"
    )
    assert len(entries) >= 1
    for e in entries:
        # The password must never appear anywhere in the audit metadata.
        assert secret not in str(e.metadata_json)


def test_rich_policy_still_guards_the_next_login_change_after_reset(db, admin, user):
    # An admin sets a temp password that satisfies min-8 but NOT the rich policy.
    user_service.reset_user_password(db, user.id, "temp-pass-123", admin)
    assert user.must_change_password is True

    # At next login the user must satisfy the RICH policy (unchanged path):
    # a weak password is rejected 422...
    with pytest.raises(HTTPException) as exc:
        auth_service.change_password(db, user, "alllowercase")
    assert exc.value.status_code == 422

    # ...and a compliant one is accepted, clearing the flag.
    updated = auth_service.change_password(db, user, "Brand-New-Pw-456!")
    assert updated.must_change_password is False
    assert verify_password("Brand-New-Pw-456!", updated.password_hash) is True
