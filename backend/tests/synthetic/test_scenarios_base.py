# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Base end-to-end scenarios of the test kit.

F01, F04, F05, F09, F15, F16 on COLL-MULTI A, through the real upload,
matching, extraction (real Tesseract OCR) and review read. Marked ``synthetic``
and excluded from the default run: ``pytest -m synthetic tests/synthetic -s``.
Each test prints a compact report.
"""
from __future__ import annotations

import shutil
import time

import pytest

from tests.synthetic.builders import CollProject, build_coll_multi
from tests.synthetic.runner import check, report, run_scenario
from tests.synthetic.scenarios import BASE_IDS, SCENARIOS

pytestmark = [
    pytest.mark.synthetic,
    pytest.mark.skipif(
        shutil.which("tesseract") is None,
        reason="Tesseract not installed: the scenarios use the real OCR",
    ),
]

_build_times: list[float] = []


@pytest.fixture
def coll_multi(db) -> CollProject:
    """The conftest's COLL-MULTI, timed: built once per test, reported once."""
    start = time.perf_counter()
    project = build_coll_multi(db)
    _build_times.append(time.perf_counter() - start)
    if len(_build_times) == 1:
        print(f"\n[synthetic] COLL-MULTI build: {_build_times[0]:.3f}s")
    return project


@pytest.mark.parametrize("scenario_id", BASE_IDS)
def test_scenario(db, coll_multi, scan_storage, scenario_id):
    scenario = SCENARIOS[scenario_id]
    outcome = run_scenario(db, coll_multi, scenario)
    notes: list[str] = []
    errors = check(outcome, coll_multi, scenario, notes)
    report(outcome, notes)
    assert not errors, f"{scenario_id}: {len(errors)} discrepancies\n" + "\n".join(
        f"  - {e}" for e in errors
    )

