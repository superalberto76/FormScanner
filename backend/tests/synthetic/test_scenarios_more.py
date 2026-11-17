# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Remaining single-pass PDF scenarios of the test kit.

F02, F03, F06, F06b, F07, F10, F11, F14, F17, F18, F19, F19b, F20 on
COLL-MULTI and F22 on COLL-SINGLE, plus F23 (the scoring case), through the
real upload, matching, extraction (real Tesseract OCR) and review read — the
same runner as the base scenarios. Marked ``synthetic`` and excluded from the
default run: ``pytest -m synthetic tests/synthetic -s``. Each test prints a
compact report.
"""
from __future__ import annotations

import shutil

import pytest

from tests.synthetic.builders import build_coll_multi, build_coll_single
from tests.synthetic.runner import check, report, run_scenario
from tests.synthetic.scenarios import MORE_IDS, SCENARIOS

pytestmark = [
    pytest.mark.synthetic,
    pytest.mark.skipif(
        shutil.which("tesseract") is None,
        reason="Tesseract not installed: the scenarios use the real OCR",
    ),
]

_BUILDERS = {"MULTI": build_coll_multi, "SINGLE": build_coll_single}


@pytest.mark.parametrize("scenario_id", MORE_IDS)
def test_scenario_more(db, scan_storage, scenario_id):
    scenario = SCENARIOS[scenario_id]
    project = _BUILDERS[scenario.project](db)
    outcome = run_scenario(db, project, scenario)
    notes: list[str] = []
    errors = check(outcome, project, scenario, notes)
    report(outcome, notes)
    assert not errors, f"{scenario_id}: {len(errors)} discrepancies\n" + "\n".join(
        f"  - {e}" for e in errors
    )

