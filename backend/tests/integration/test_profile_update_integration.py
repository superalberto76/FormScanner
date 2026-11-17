# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for self-service profile edit.

Exercises the ``PATCH /auth/me`` handler directly (``auth.update_me``), matching
the style of ``test_change_password_integration.py``. Requires TEST_DATABASE_URL —
see conftest.py (the shared ``db``/``user`` fixtures).

Covers:
* first/last are updated and returned.
* An empty body is a no-op (existing names preserved).
* A partial body updates only the supplied field.
* role/disabled/email cannot be changed even when injected into the request body.
"""
from __future__ import annotations

from app.api.v1.endpoints.auth import me, update_me
from app.models.enums import UITheme, UserRole
from app.schemas.user import UserProfileUpdate


def test_update_me_sets_first_and_last(db, user):
    user.first_name = "Old"
    user.last_name = "Name"
    db.flush()

    result = update_me(
        UserProfileUpdate(first_name="Mario", last_name="Rossi"),
        current_user=user,
        db=db,
    )

    assert result.first_name == "Mario"
    assert result.last_name == "Rossi"


def test_update_me_empty_body_is_noop(db, user):
    user.first_name = "Keep"
    user.last_name = "Same"
    db.flush()

    result = update_me(UserProfileUpdate(), current_user=user, db=db)

    assert result.first_name == "Keep"
    assert result.last_name == "Same"


def test_update_me_partial_only_touches_supplied_field(db, user):
    user.first_name = "First"
    user.last_name = "Last"
    db.flush()

    result = update_me(
        UserProfileUpdate(first_name="Changed"), current_user=user, db=db
    )

    assert result.first_name == "Changed"
    assert result.last_name == "Last"  # untouched


def test_update_me_cannot_change_role_disabled_email(db, user):
    user.first_name = "Mario"
    user.last_name = "Rossi"
    user.role = UserRole.USER
    user.disabled = False
    original_email = user.email
    db.flush()

    # Even if a hostile client injects protected fields, the schema drops them so
    # the endpoint never applies them.
    payload = UserProfileUpdate.model_validate(
        {
            "first_name": "Mario",
            "last_name": "Rossi",
            "role": "ADMIN",
            "disabled": True,
            "email": "attacker@example.com",
        }
    )
    result = update_me(payload, current_user=user, db=db)

    assert result.role is UserRole.USER
    assert result.disabled is False
    assert result.email == original_email


def test_update_me_persists_ui_theme_and_get_me_echoes_it(db, user):
    # PATCH {ui_theme:"DARK"} persists and is echoed by GET /auth/me.
    assert user.ui_theme is UITheme.SYSTEM  # default

    result = update_me(
        UserProfileUpdate(ui_theme=UITheme.DARK), current_user=user, db=db
    )
    assert result.ui_theme is UITheme.DARK

    # Re-read from the DB (the same row the GET handler returns) to prove it stuck.
    db.expire(user)
    echoed = me(current_user=user)
    assert echoed.ui_theme is UITheme.DARK


def test_update_me_ui_theme_does_not_touch_names(db, user):
    user.first_name = "Mario"
    user.last_name = "Rossi"
    db.flush()

    result = update_me(
        UserProfileUpdate(ui_theme=UITheme.LIGHT), current_user=user, db=db
    )

    assert result.ui_theme is UITheme.LIGHT
    assert result.first_name == "Mario"  # untouched
    assert result.last_name == "Rossi"
