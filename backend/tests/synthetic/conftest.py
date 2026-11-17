# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fixtures for the test-kit projects built through the services.

``engine`` / ``db`` are the integration ones (real Postgres, SAVEPOINT
isolation, skipped without TEST_DATABASE_URL); each COLL-* fixture builds its
project inside that rolled-back transaction.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import settings
from app.services import review_action_service, submission_lock_service
from tests.integration.conftest import engine, db  # noqa: F401
from tests.synthetic.builders import (
    CollProject,
    build_coll_config,
    build_coll_multi,
    build_coll_single,
)


@pytest.fixture
def coll_multi(db) -> CollProject:  # noqa: F811
    return build_coll_multi(db)


@pytest.fixture
def coll_single(db) -> CollProject:  # noqa: F811
    return build_coll_single(db)


@pytest.fixture
def coll_config(db) -> CollProject:  # noqa: F811
    return build_coll_config(db)


class Clock:
    """A settable stand-in for the review clocks, moved by hand."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def clock(monkeypatch) -> Clock:
    """``submission_lock_service._now`` (lock and its expiry) and
    ``review_action_service._now`` (``reviewed_at``) on one fixed UTC instant."""
    c = Clock(datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(submission_lock_service, "_now", c)
    monkeypatch.setattr(review_action_service, "_now", c)
    return c


@pytest.fixture
def scan_storage(tmp_path, monkeypatch):
    """Scanned pages and originals under *tmp_path*. The OCR is the real one."""
    monkeypatch.setattr(settings, "submission_pages_dir", str(tmp_path / "pages"))
    monkeypatch.setattr(
        settings, "submission_originals_dir", str(tmp_path / "originals")
    )


@pytest.fixture(autouse=True)
def ocr_language_ita(monkeypatch):
    """The OCR reads in Italian here whatever the environment configures:
    the sheets are Italian, and the outcome must not depend on the CI running
    with ``ita`` or ``eng``."""
    monkeypatch.setattr(settings, "ocr_language", "ita")
