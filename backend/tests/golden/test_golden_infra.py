# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The golden set's infrastructure.

Not the golden-set test itself (``test_golden_set.py``). Here: the scans are
the expected ones and within budget; the geometry fingerprint is stable across
exports and builds of the same code; a PDF goes through ``run_pdf`` exactly as
through a scenario, checked on the synthetic F01; the observed table is built
from that run; ``truth.json`` is consistent with the cases and COLL-MULTI, and
a known limit read without review is an error. These run on synthetic sheets,
so the code is verified even when a scan changes.

Marked ``golden`` and excluded from the default run:
``pytest -m golden tests/golden``.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import replace

import fitz
import pytest
from sqlalchemy import select

from app.models.enums import PageAlignmentStatus, Validity
from app.models.submission_page import SubmissionPage
from app.utils import alignment_marks

from tests.golden.golden import (
    CASES,
    CASES_BY_ID,
    KNOWN_LIMIT,
    MAX_SCANS_BYTES,
    NO_SIDE_MARKS_DIR,
    NO_SIDE_MARKS_SCAN,
    PAGES_PER_SCAN,
    SCANS_DIR,
    TruthObject,
    case_expectation,
    load_truth,
    observed_rows,
    page_marks,
    project_fingerprint,
    render,
    run_case,
    truth_problems,
)
from tests.synthetic.builders import build_coll_multi
from tests.synthetic.generator import export_template
from tests.synthetic.runner import check, run_pdf, scenario_pdf
from tests.synthetic.scenarios import (
    ALWAYS_REVIEW,
    INVALID_ANSWER,
    LOW_CONFIDENCE,
    SCENARIOS,
    Deviation,
    ExpectedScore,
    QuestionReading,
    RespondentIdReading,
)

pytestmark = pytest.mark.golden

needs_tesseract = pytest.mark.skipif(
    shutil.which("tesseract") is None,
    reason="Tesseract not installed: extraction uses the real OCR",
)


def test_scans_are_the_expected_ones_and_within_budget():
    on_disk = sorted(p.name for p in SCANS_DIR.glob("*.pdf"))
    assert on_disk == sorted(c.path.name for c in CASES)
    total = 0
    for case in CASES:
        total += case.path.stat().st_size
        with fitz.open(case.path) as doc:
            assert doc.page_count == PAGES_PER_SCAN, f"{case.id}: {doc.page_count} pages"
    # the scan without side marks counts in the same budget
    assert sorted(p.name for p in NO_SIDE_MARKS_DIR.glob("*.pdf")) == [NO_SIDE_MARKS_SCAN.name]
    total += NO_SIDE_MARKS_SCAN.stat().st_size
    assert total <= MAX_SCANS_BYTES, f"scans: {total} bytes, cap {MAX_SCANS_BYTES}"


def test_fingerprint_is_stable_across_exports_and_builds(db, coll_multi):
    first = project_fingerprint(db, coll_multi)
    assert project_fingerprint(db, coll_multi) == first  # a second export
    assert project_fingerprint(db, build_coll_multi(db)) == first  # another build


def test_fingerprint_covers_the_alignment_marks(db, coll_multi):
    # four marks and the hole of the distinct one per page, and the side
    # marks, left and right
    pdf, _geometry = export_template(db, coll_multi, "A")
    marks = page_marks(pdf)
    assert sorted(marks) == [1, 2, 3]
    expected = 5 + 2 * alignment_marks.SIDE_MARK_COUNT
    assert all(len(found) == expected for found in marks.values())


@needs_tesseract
@pytest.mark.parametrize(
    "case_id, stretched",
    [("F01", True), ("F21a", True), ("F21b", False)],
)
def test_real_scans_are_straightened_on_the_side_marks(
    db, coll_multi, scan_storage, case_id, stretched
):
    """F01 and F21a printed with the side marks and scanned from the ADF,
    F21b on the flatbed turned by about 3°. Every page ALIGNED and
    straightened strip by strip, nothing discarded; from the ADF the measured
    stretch is above 1 mm, on the flatbed it stays a few tenths."""
    outcome = run_case(db, coll_multi, CASES_BY_ID[case_id])
    pages = db.scalars(
        select(SubmissionPage).where(SubmissionPage.submission_id == outcome.submission_id)
    ).all()
    assert len(pages) == PAGES_PER_SCAN
    for page in pages:
        assert page.alignment_status is PageAlignmentStatus.ALIGNED, page.alignment_json
        strips = page.alignment_json["strips"]
        assert strips["applied"] is True and strips["reason"] is None, strips
        nodes = [n for column in strips["columns"].values() for n in column]
        assert not any(n["rejected"] for n in nodes)
        assert all(n["found"] is not None for n in nodes if not n["corner"])
        if stretched:
            assert strips["max_shift_mm"] > 0.8
        else:
            assert strips["max_shift_mm"] < 0.4


@needs_tesseract
def test_an_adf_scan_printed_without_side_marks_keeps_the_affine_image(
    db, coll_multi, scan_storage
):
    """F01 printed with four corner marks and no side mark, scanned from the
    ADF. Every page still ALIGNED on its
    corners; the strip correction not applied, for the reason that says no
    side mark is there — never a page NOT aligned, never a review reason."""
    outcome = run_pdf(db, coll_multi, "F01-no-side-marks", NO_SIDE_MARKS_SCAN.read_bytes())
    pages = db.scalars(
        select(SubmissionPage).where(SubmissionPage.submission_id == outcome.submission_id)
    ).all()
    assert len(pages) == PAGES_PER_SCAN
    for page in pages:
        assert page.alignment_status is PageAlignmentStatus.ALIGNED, page.alignment_json
        strips = page.alignment_json["strips"]
        assert strips["applied"] is False
        assert strips["reason"] == "SIDE_MARKERS_NOT_FOUND"
        side = [n for column in strips["columns"].values() for n in column if not n["corner"]]
        assert len(side) == 2 * alignment_marks.SIDE_MARK_COUNT
        assert all(n["found"] is None for n in side)
    assert not any(r.sheet_not_aligned for r in outcome.results.values())


@needs_tesseract
def test_run_pdf_reads_a_pdf_like_a_scenario(db, coll_multi, scan_storage):
    scenario = SCENARIOS["F01"]
    outcome = run_pdf(db, coll_multi, "F01", scenario_pdf(db, coll_multi, scenario))

    errors = check(outcome, coll_multi, scenario)
    assert not errors, "\n".join(errors)

    rows = observed_rows(outcome, coll_multi)
    by_key = {r.key: r for r in rows}
    assert by_key["Q05"].reading == "A,C"
    assert by_key["M-RIGHE"].reading == "r1c1 r2c2 r3c3 r4c4"
    assert by_key["MATRICOLA"].reading == "'100001'"
    assert by_key["Q01"].review == ""
    assert "ALWAYS_REVIEW" in by_key["NOME"].review
    assert [r.page for r in rows] == sorted(r.page for r in rows)

    text = render(CASES_BY_ID["F01"], outcome, rows, errors)
    assert "comparison with truth.json: 0 discrepancies" in text


# ---------------------------------------------------------------------------
# Truth sheet and expectations
# ---------------------------------------------------------------------------


def test_truth_is_consistent_with_the_cases_and_coll_multi(coll_multi):
    # Not the fingerprint: a layout change fails test_golden_set with
    # "reprint it", not this test.
    assert truth_problems(load_truth(), coll_multi) == []


def test_case_expectation_starts_from_f01():
    truth = load_truth()
    base = SCENARIOS["F01"].expected

    f21a, loose = case_expectation(truth, "F21a")
    assert loose == frozenset()
    assert f21a.expected.candidate == "100021"
    assert f21a.expected.readings == {**base.readings, "MATRICOLA": RespondentIdReading("100021")}
    assert f21a.expected.review == base.review

    f08, loose = case_expectation(truth, "F08")
    assert loose == frozenset()
    assert f08.expected.readings["Q01"] == QuestionReading((), Validity.NO_ANSWER)
    assert f08.expected.review["Q01"] == frozenset({INVALID_ANSWER})
    assert "Q02" not in f08.expected.review
    assert (f08.expected.score.points, f08.expected.score.max) == (27, 28)

    f13, loose = case_expectation(truth, "F13")
    assert loose == {"NOME", "APERTA"}
    assert f13.expected.free_texts == ()
    assert "NOME" not in f13.expected.review and "NOME" not in f13.expected.readings


def test_a_known_limit_cannot_carry_a_reading(tmp_path):
    truth = json.loads((SCANS_DIR.parent / "truth.json").read_text(encoding="utf-8"))
    truth["cases"]["F07"]["objects"]["Q01"]["reading"] = {"letters": ["B"], "validity": "VALID"}
    path = tmp_path / "truth.json"
    path.write_text(json.dumps(truth), encoding="utf-8")
    with pytest.raises(ValueError, match="known limit"):
        load_truth(path)


def test_truth_problems_names_what_is_wrong(coll_multi):
    truth = load_truth()
    f08 = truth.cases["F08"]
    broken = replace(truth, cases={**truth.cases, "F08": replace(f08, objects={
        **f08.objects,
        "Q99": TruthObject(KNOWN_LIMIT, "label that does not exist"),
        "M-MULTI": replace(f08.objects["Q02"]),
        "Q03": replace(f08.objects["Q02"], reading=QuestionReading(("Z",), Validity.VALID)),
        "Q04": replace(f08.objects["Q01"], review=frozenset({"INVALID_ANSWR"})),
    }, score=ExpectedScore(30, 30))})
    problems = truth_problems(broken, coll_multi)
    assert any("Q99" in p for p in problems)
    assert any("M-MULTI: requirement on MATRIX" in p for p in problems)
    assert any("Q03: letters ['Z']" in p for p in problems)
    assert any("Q04: unknown review reasons ['INVALID_ANSWR']" in p for p in problems)
    assert any("F08: max 30, that of F01 is 28" in p for p in problems)


@needs_tesseract
def test_check_with_known_limits_on_the_synthetic_f01(db, coll_multi, scan_storage):
    """``check(loose=...)``: a known limit in review is only noted, with its
    reading; one read without review is the error that marks an app bug or a
    reading to reclassify; a free text among them must keep ALWAYS_REVIEW."""
    scenario = SCENARIOS["F01"]
    outcome = run_pdf(db, coll_multi, "F01", scenario_pdf(db, coll_multi, scenario))
    truth = load_truth()

    # F01 from truth.json: no difference from the scenario
    f01, loose = case_expectation(truth, "F01")
    assert check(outcome, coll_multi, f01, loose=loose) == []

    # NOME as a known limit: in review (ALWAYS_REVIEW), so only a note
    expected = scenario.expected
    name_loose = replace(scenario, expected=replace(
        expected,
        review={k: v for k, v in expected.review.items() if k != "NOME"},
        free_texts=tuple(t for t in expected.free_texts if t != "NOME"),
    ))
    notes: list[str] = []
    assert check(outcome, coll_multi, name_loose, notes, loose=frozenset({"NOME"})) == []
    assert any(
        # the OCR text itself is not compared
        n.startswith("known limit NOME: read '") and n.endswith(f"in review ({ALWAYS_REVIEW})")
        for n in notes
    ), notes

    # the same, with NOME in review for another reason only: ALWAYS_REVIEW missing
    detail = outcome.detail
    without_always = replace(outcome, detail=detail.model_copy(update={"pages": [
        page.model_copy(update={"objects": [
            o.model_copy(update={"flag_reasons": [LOW_CONFIDENCE]}) if o.label == "NOME" else o
            for o in page.objects
        ]})
        for page in detail.pages
    ]}))
    errors = check(without_always, coll_multi, name_loose, loose=frozenset({"NOME"}))
    assert errors == ["known limit NOME: ALWAYS_REVIEW missing (reasons: ['LOW_CONFIDENCE'])"]

    # Q01 as a known limit: read cleanly, not in review → the bug message
    readings = dict(expected.readings)
    del readings["Q01"]
    q01_loose = replace(scenario, expected=replace(expected, readings=readings))
    errors = check(outcome, coll_multi, q01_loose, loose=frozenset({"Q01"}))
    assert errors == [
        "known limit Q01 read without review: a bug, or a correct reading "
        "to reclassify as a requirement (read B)"
    ]

    # a label both loose and expected exactly is a mistake in the caller
    with pytest.raises(ValueError, match="Q02"):
        check(outcome, coll_multi, scenario, loose=frozenset({"Q02"}))
    # ...also when it is only under a deviation
    deviated = replace(scenario, expected=replace(
        expected, readings=readings,
        deviations={"reading:Q01": Deviation("#000", None)},
    ))
    with pytest.raises(ValueError, match="Q01"):
        check(outcome, coll_multi, deviated, loose=frozenset({"Q01"}))
