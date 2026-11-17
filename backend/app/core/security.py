# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Password hashing, the password policy, and JWT token utilities."""
from __future__ import annotations

import string
import uuid
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings
from app.models.enums import UserRole

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ---------------------------------------------------------------------------
# Password policy — the SINGLE SOURCE OF TRUTH
# ---------------------------------------------------------------------------
# The frontend mirrors this in `frontend/src/auth/passwordPolicy.ts`. The
# special-character set below MUST stay byte-for-byte identical to the FE
# string; freeze tests on both sides assert against the same frozen literal:
#   backend/tests/test_password_policy.py
#   frontend/src/auth/passwordPolicy.test.ts
# Do not edit one side without the other.

PASSWORD_MIN_LENGTH = 8

# Allowed special characters (ASCII). Note plain ' and " are members; the
# backslash is escaped once. Membership is tested as a set to avoid the
# escaping bugs a regex character class would invite.
PASSWORD_SPECIAL_CHARS = "@#$%^&*-_!+=[]{}|\\:',.?/`~\"()<>;"

_PASSWORD_SPECIAL_SET = frozenset(PASSWORD_SPECIAL_CHARS)


def password_policy_errors(pw: str) -> list[str]:
    """Return one human-readable message per FAILED policy rule.

    An empty list means *pw* satisfies the full policy. Messages are stable:
    they feed the change-password 422 response and are mirrored (conceptually)
    by the FE checklist. Confirmation matching is a FE-only concern.
    """
    errors: list[str] = []
    if len(pw) < PASSWORD_MIN_LENGTH:
        errors.append(
            f"Password must be at least {PASSWORD_MIN_LENGTH} characters long"
        )
    if not any(c in string.digits for c in pw):
        errors.append("Password must contain at least one digit")
    if not any(c in string.ascii_uppercase for c in pw):
        errors.append("Password must contain at least one uppercase letter")
    if not any(c in string.ascii_lowercase for c in pw):
        errors.append("Password must contain at least one lowercase letter")
    if not any(c in _PASSWORD_SPECIAL_SET for c in pw):
        errors.append("Password must contain at least one special character")
    return errors


def is_password_valid(pw: str) -> bool:
    """True when *pw* satisfies every policy rule (no confirmation check)."""
    return not password_policy_errors(pw)


def hash_password(plaintext: str) -> str:
    return _pwd_context.hash(plaintext)


def verify_password(plaintext: str, hashed: str) -> bool:
    return _pwd_context.verify(plaintext, hashed)


def create_access_token(user_id: uuid.UUID, role: UserRole) -> str:
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.access_token_expires_minutes
    )
    payload = {
        "sub": str(user_id),
        "role": role.value,
        "exp": expire,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    """Decode and verify a JWT.  Raises JWTError on any failure."""
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
