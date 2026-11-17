# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fixtures of the golden set: the synthetic ones, reused.

``db`` is the integration fixture (real Postgres, rolled back, skipped without
TEST_DATABASE_URL); ``coll_multi`` builds COLL-MULTI from the same spec the
sheets were printed from; ``scan_storage`` keeps scanned pages and originals
under a temporary folder; ``ocr_language_ita`` (autouse) pins the OCR to
Italian.
"""
import pytest

from app.core.config import settings
from tests.integration.conftest import engine, db  # noqa: F401
from tests.synthetic.conftest import coll_multi, scan_storage  # noqa: F401


@pytest.fixture(autouse=True)
def ocr_language_ita(monkeypatch):
    """The OCR reads in Italian here whatever the environment configures: the
    sheets are Italian, and the outcome must not depend on the environment
    running with ``ita`` or ``eng``."""
    monkeypatch.setattr(settings, "ocr_language", "ita")
