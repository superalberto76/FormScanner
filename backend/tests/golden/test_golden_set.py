# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The golden set: real scans against ``truth.json``.

For each case: first the geometry fingerprint — a scan printed with another
layout fails with "reprint it" — then upload, matching, extraction and the
review read, compared with the expectation built from F01 and the case's
differences. Requirements are checked strictly; a known limit only has to be
in review, and what was read is reported (``-s`` shows the notes).

Marked ``golden`` and excluded from the default run:
``pytest -m golden tests/golden``.
"""
from __future__ import annotations

import shutil

import pytest

from tests.golden.golden import (
    CASES,
    REPRINT_MESSAGE,
    case_expectation,
    load_truth,
    project_fingerprint,
    run_case,
)
from tests.synthetic.runner import check

pytestmark = pytest.mark.golden

needs_tesseract = pytest.mark.skipif(
    shutil.which("tesseract") is None,
    reason="Tesseract not installed: extraction uses the real OCR",
)


@pytest.fixture(scope="module")
def truth():
    return load_truth()


@pytest.fixture(scope="module")
def fingerprint_cache() -> dict[str, str]:
    # The export is the same for every case: computed once per run
    return {}


@needs_tesseract
@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_golden_case(db, coll_multi, scan_storage, truth, fingerprint_cache, case):
    if "value" not in fingerprint_cache:
        fingerprint_cache["value"] = project_fingerprint(db, coll_multi)
    fingerprint = fingerprint_cache["value"]
    assert fingerprint == truth.fingerprint, (
        f"{REPRINT_MESSAGE} (fingerprint of the code {fingerprint}, "
        f"of truth.json {truth.fingerprint})"
    )

    scenario, loose = case_expectation(truth, case.id)
    outcome = run_case(db, coll_multi, case)
    notes: list[str] = []
    errors = check(outcome, coll_multi, scenario, notes, loose=loose)
    print(f"\n{case.id}: " + ("; ".join(notes) if notes else "no notes"))
    assert not errors, f"{case.id}:\n" + "\n".join(errors)
