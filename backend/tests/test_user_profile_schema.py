# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure (DB-free) tests for the self-service profile schema.

These guard the most important invariant of ``PATCH /auth/me``: a self-service
user can edit only their own name. ``role``/``disabled``/``email`` must be
unreachable — even if a caller injects them into the JSON body, Pydantic drops
the extra keys, so the endpoint never sees them.

No DB required (runs anywhere, like ``test_password_policy.py``).
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.enums import UITheme
from app.schemas.user import UserProfileUpdate


def test_profile_update_ignores_protected_fields():
    # A hostile body trying to escalate role / un-disable / change login email.
    data = UserProfileUpdate.model_validate(
        {
            "first_name": "Mario",
            "last_name": "Rossi",
            "role": "ADMIN",
            "disabled": True,
            "email": "attacker@example.com",
        }
    )
    dumped = data.model_dump(exclude_unset=True)

    # Only the two editable fields survive; the injected ones are dropped entirely.
    assert dumped == {"first_name": "Mario", "last_name": "Rossi"}
    assert not hasattr(data, "role")
    assert not hasattr(data, "disabled")
    assert not hasattr(data, "email")


def test_empty_body_dumps_to_no_changes():
    data = UserProfileUpdate()
    assert data.model_dump(exclude_unset=True) == {}


def test_single_field_is_a_partial_update():
    data = UserProfileUpdate.model_validate({"first_name": "Solo"})
    assert data.model_dump(exclude_unset=True) == {"first_name": "Solo"}


def test_blank_name_is_rejected():
    # min_length=1: an empty string is not a valid name (use omission for no-op).
    with pytest.raises(ValidationError):
        UserProfileUpdate(first_name="")


def test_ui_theme_is_an_editable_field():
    # ui_theme is the only non-name field a self-service user may set.
    data = UserProfileUpdate.model_validate({"ui_theme": "DARK"})
    dumped = data.model_dump(exclude_unset=True)
    assert dumped == {"ui_theme": UITheme.DARK}


def test_ui_theme_survives_alongside_dropped_protected_fields():
    # ui_theme is applied; role/disabled/email are still unreachable.
    data = UserProfileUpdate.model_validate(
        {"ui_theme": "SYSTEM", "role": "ADMIN", "disabled": True, "email": "x@y.z"}
    )
    assert data.model_dump(exclude_unset=True) == {"ui_theme": UITheme.SYSTEM}
    assert not hasattr(data, "role")
    assert not hasattr(data, "disabled")
    assert not hasattr(data, "email")


def test_invalid_ui_theme_is_rejected():
    with pytest.raises(ValidationError):
        UserProfileUpdate(ui_theme="BLUE")
