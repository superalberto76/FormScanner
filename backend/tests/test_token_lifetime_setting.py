# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The access-token lifetime is read from ACCESS_TOKEN_EXPIRE_MINUTES, the name
documented in .env.example and passed by docker-compose.yml, and still from
ACCESS_TOKEN_EXPIRES_MINUTES, the name the backend used to read."""
from datetime import datetime, timezone
import uuid

from jose import jwt

from app.core import security
from app.core.config import Settings, settings
from app.models.enums import UserRole

_NAMES = ("ACCESS_TOKEN_EXPIRE_MINUTES", "ACCESS_TOKEN_EXPIRES_MINUTES")


def _clear(monkeypatch):
    for name in _NAMES:
        monkeypatch.delenv(name, raising=False)


def test_the_default_lifetime_is_480_minutes(monkeypatch):
    _clear(monkeypatch)

    assert Settings(_env_file=None).access_token_expires_minutes == 480


def test_the_documented_name_is_read(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ACCESS_TOKEN_EXPIRE_MINUTES", "15")

    assert Settings(_env_file=None).access_token_expires_minutes == 15


def test_the_previous_name_is_still_read(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ACCESS_TOKEN_EXPIRES_MINUTES", "30")

    assert Settings(_env_file=None).access_token_expires_minutes == 30


def test_the_documented_name_wins_when_both_are_set(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ACCESS_TOKEN_EXPIRE_MINUTES", "15")
    monkeypatch.setenv("ACCESS_TOKEN_EXPIRES_MINUTES", "30")

    assert Settings(_env_file=None).access_token_expires_minutes == 15


def test_the_documented_name_is_read_from_the_env_file(monkeypatch, tmp_path):
    _clear(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text("ACCESS_TOKEN_EXPIRE_MINUTES=45\n", encoding="utf-8")

    assert Settings(_env_file=env_file).access_token_expires_minutes == 45


def test_the_token_expires_after_the_configured_lifetime(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ACCESS_TOKEN_EXPIRE_MINUTES", "15")
    monkeypatch.setattr(
        settings,
        "access_token_expires_minutes",
        Settings(_env_file=None).access_token_expires_minutes,
    )

    before = datetime.now(timezone.utc).timestamp()
    token = security.create_access_token(uuid.uuid4(), UserRole.USER)
    claims = jwt.get_unverified_claims(token)

    assert 15 * 60 - 5 <= claims["exp"] - before <= 15 * 60 + 5
