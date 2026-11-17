# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Freeze + behaviour tests for the password policy.

DB-free unit tests: they exercise only the pure policy in
``app.core.security`` and run anywhere ``pytest`` does.

ANTI-DIVERGENCE: the special-char set MUST stay byte-for-byte identical to the
frontend mirror in ``frontend/src/auth/passwordPolicy.ts`` (which carries the
SAME frozen literal in its own ``passwordPolicy.test.ts``).
"""
from __future__ import annotations

from app.core.security import (
    PASSWORD_SPECIAL_CHARS,
    is_password_valid,
    password_policy_errors,
)

# The frozen literal — keep identical to the FE constant.
FROZEN_SPECIAL = "@#$%^&*-_!+=[]{}|\\:',.?/`~\"()<>;"


def test_special_chars_frozen_and_unique():
    assert PASSWORD_SPECIAL_CHARS == FROZEN_SPECIAL
    assert len(PASSWORD_SPECIAL_CHARS) == 32
    # No duplicate characters slipped into the set.
    assert len(set(PASSWORD_SPECIAL_CHARS)) == len(PASSWORD_SPECIAL_CHARS)


def test_compliant_password_has_no_errors():
    assert password_policy_errors("Abcdef1@") == []
    assert is_password_valid("Abcdef1@") is True


def test_too_short_is_rejected():
    # has upper/lower/digit/special but only 4 chars
    errors = password_policy_errors("Ab1@")
    assert any("at least" in e and "characters" in e for e in errors)
    assert is_password_valid("Ab1@") is False


def test_missing_digit_is_rejected():
    errors = password_policy_errors("Abcdefg@")
    assert any("digit" in e for e in errors)


def test_missing_uppercase_is_rejected():
    errors = password_policy_errors("abcdef1@")
    assert any("uppercase" in e for e in errors)


def test_missing_lowercase_is_rejected():
    errors = password_policy_errors("ABCDEF1@")
    assert any("lowercase" in e for e in errors)


def test_missing_special_is_rejected():
    errors = password_policy_errors("Abcdefg1")
    assert any("special" in e for e in errors)


def test_every_special_char_satisfies_the_special_rule():
    # A base that satisfies all other rules; appending any special char must
    # make the whole password valid (proves each listed char is recognised).
    for ch in PASSWORD_SPECIAL_CHARS:
        assert is_password_valid(f"Abcdef1{ch}") is True, f"char {ch!r} not accepted"
