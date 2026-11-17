# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The test kit export.

The export writes the files the scenarios upload and a truth sheet from their
declarations; these tests check the tree, a few rows the test kit spells out,
and that a PDF of the kit does not depend on the database it was built in (the
premise of matching the kit against projects seeded in a deployed
environment). The full export is marked ``synthetic`` (~1.5 min, no OCR):
``pytest -m synthetic tests/synthetic/test_export_kit.py``.
"""
from __future__ import annotations

import csv
import io
from dataclasses import replace

import pytest

from app.core.config import settings
from tests.synthetic.builders import build_coll_multi
from tests.synthetic.export_kit import (
    OBJECTS_COLUMNS,
    OBJECTS_CSV,
    SUBMISSIONS_COLUMNS,
    SUBMISSIONS_CSV,
    check_deviations,
    check_out_dir,
    export_kit,
)
from tests.synthetic.runner import scenario_pdf
from tests.synthetic.scenarios import LOOSE_CASES, SCENARIOS, Deviation

# The kit, file by file.
EXPECTED_TREE = {
    *(
        f"1-kit-pdf/{case}.pdf"
        for case in (
            "F01", "F02", "F03", "F04", "F05", "F06", "F06b", "F07", "F09", "F10",
            "F11", "F14", "F15", "F16", "F17", "F18", "F19", "F19b", "F20", "F23",
        )
    ),
    "2-kit-images-multi/verdi_pagina_1.png",
    "2-kit-images-multi/verdi_pagina_2.png",
    "2-kit-images-multi/verdi_pagina_3.png",
    "2-kit-images-multi/U01_report.docx",
    "2-kit-images-multi/U02_empty.pdf",
    "2-kit-images-multi/U02_corrupt.pdf",
    "2-kit-images-multi/U03_too-large.pdf",
    "3-kit-images-single/G03/upload-1/gialli_pagina_1.png",
    "3-kit-images-single/G03/upload-2/gialli_pagina_2.png",
    "3-kit-images-single/G04/a/bianchi_pagina_1.jpg",
    "3-kit-images-single/G04/b/bianchi_pagina_1.jpg",
    "3-kit-images-single/G05/IMG_20261022_0042.png",
    "4-kit-single/F22.pdf",
    "4-kit-single/rossi-mario_pagina_1.jpg",
    "4-kit-single/rossi-mario_pagina_2.jpg",
    "truth_objects.csv",
    "truth_submissions.csv",
}


def _read_csv(data: bytes) -> tuple[list[str], list[dict[str, str]]]:
    assert data.startswith(b"\xef\xbb\xbf"), "CSV without UTF-8 BOM"
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
    return list(reader.fieldnames or []), list(reader)


def _row(rows, **match) -> dict[str, str]:
    found = [r for r in rows if all(r[k] == v for k, v in match.items())]
    assert len(found) == 1, f"{match}: {len(found)} rows"
    return found[0]


@pytest.mark.synthetic
def test_export_kit(db, tmp_path):
    out = tmp_path / "kit"
    summary = export_kit(db, out)

    # 1. the tree
    written = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
    assert written == EXPECTED_TREE
    assert sorted(summary.files) == sorted(EXPECTED_TREE)

    # 2. a kit PDF is the scenario's, whatever project it was built in
    other_multi = build_coll_multi(db)
    assert (out / "1-kit-pdf/F01.pdf").read_bytes() == scenario_pdf(
        db, other_multi, SCENARIOS["F01"]
    )

    # 3. upload errors
    assert (out / "2-kit-images-multi/U02_empty.pdf").read_bytes() == b""
    oversize = (out / "2-kit-images-multi/U03_too-large.pdf").read_bytes()
    assert len(oversize) > settings.max_submission_pdf_bytes
    assert oversize.startswith(b"%PDF")

    # 4. the submissions sheet: every case once, in kit order
    columns, cases = _read_csv((out / SUBMISSIONS_CSV).read_bytes())
    assert tuple(columns) == SUBMISSIONS_COLUMNS
    expected_ids = {*SCENARIOS, *LOOSE_CASES, "U01", "U02", "U03"}
    assert sorted(r["case"] for r in cases) == sorted(expected_ids)
    assert [r["case"] for r in cases][:3] == ["F01", "F02", "F03"]
    assert len(cases) == summary.submission_rows
    f15 = _row(cases, case="F15")
    assert (f15["expected_missing_pages"], f15["expected_points"], f15["expected_max"]) == (
        "2", "17", "28",
    )
    f23 = _row(cases, case="F23")
    assert (f23["expected_status"], f23["expected_points"], f23["expected_max"]) == (
        "SCANNED", "24", "28",
    )
    f18 = _row(cases, case="F18")
    assert (f18["status_after_upload"], f18["expected_points"], f18["variant"]) == (
        "MATCH_FAILED", "no score", "none (printed as A)",
    )
    assert "28 / 28" in f18["notes"]
    f22 = _row(cases, case="F22")
    assert (f22["project"], f22["batch"]) == ("COLL-SINGLE", "4-kit-single")
    g05 = _row(cases, case="G05")
    # Missing pages of G05: not declared by the test kit, left empty.
    assert (g05["status_after_upload"], g05["expected_missing_pages"]) == ("UNASSIGNED", "")
    assert _row(cases, case="G04")["expected_missing_pages"] == "2"
    for r in cases:
        assert all(
            r[c] == "" for c in SUBMISSIONS_COLUMNS if c.startswith("observed_")
        ), r["case"]
        assert r["outcome"] == ""

    # 5. the objects sheet
    columns, objects = _read_csv((out / OBJECTS_CSV).read_bytes())
    assert tuple(columns) == OBJECTS_COLUMNS
    assert len(objects) == summary.object_rows
    for scenario in SCENARIOS.values():
        e = scenario.expected
        keys = {*e.readings, *e.free_texts, *e.review}
        assert len([r for r in objects if r["case"] == scenario.id]) == len(keys), scenario.id
    assert not [r for r in objects if r["object"] == "VID"]

    q01 = _row(objects, case="F01", object="Q01")
    assert (q01["page"], q01["sheet"], q01["marked_on_paper"], q01["expected_reading"]) == (
        "1", "1", "B full", "B",
    )
    assert (q01["expected_review"], q01["expected_reason"]) == ("no", "")
    f04 = _row(objects, case="F04", object="Q01")
    assert (f04["expected_reading"], f04["expected_review"], f04["expected_reason"]) == (
        "A,B (TOO_MANY_ANSWERS)", "yes", "INVALID_ANSWER",
    )
    f09 = _row(objects, case="F09", object="MATRICOLA")
    assert (f09["marked_on_paper"], f09["expected_reading"], f09["expected_reason"]) == (
        "1 0 0 (0 and 3) 0 9", "100009 (ambiguous columns: 4)", "AMBIGUOUS_DIGITS",
    )
    f19 = _row(objects, case="F19", object="Q10", sheet="4")
    assert (f19["page"], f19["expected_reading"]) == ("3", "A")
    assert _row(objects, case="F19", object="Q10", sheet="3")["expected_reading"] == "D"
    f14 = _row(objects, case="F14", object="BARCODE")
    assert (f14["marked_on_paper"], f14["expected_reading"], f14["expected_reason"]) == (
        "covered", "not decoded", "DECODE_FAILED",
    )
    f07 = _row(objects, case="F07", object="Q03")
    assert (f07["marked_on_paper"], f07["expected_reading"]) == (
        "A cross", "none (NO_ANSWER)",
    )
    # Loose images: the sheet is the file that holds it (G01 names are
    # deliberately wrong, G02 is uploaded page 2 first).
    g01 = _row(objects, case="G01", object="Q01")
    assert (g01["page"], g01["sheet"]) == ("1", "verdi_pagina_3.png")
    g02 = _row(objects, case="G02", object="Q04")
    assert (g02["page"], g02["sheet"]) == ("2", "rossi-mario_pagina_2.jpg")
    name = _row(objects, case="F01", object="NOME")
    assert (name["expected_review"], name["expected_reason"]) == ("yes", "ALWAYS_REVIEW")
    for r in objects:
        assert r["observed_reading"] == r["observed_review"] == r["observed_reason"] == ""
        assert r["outcome"] == ""


def test_out_dir_must_be_empty(tmp_path):
    """A new or empty folder is fine; one with files is refused."""
    check_out_dir(tmp_path / "new")
    check_out_dir(tmp_path)
    (tmp_path / "old.pdf").write_bytes(b"x")
    with pytest.raises(SystemExit, match="is not an empty folder"):
        check_out_dir(tmp_path)


def test_unhandled_deviation_stops_the_export():
    """A deviation the truth sheet does not report must not vanish silently."""
    f01 = SCENARIOS["F01"]
    for scenario in SCENARIOS.values():
        check_deviations(scenario)  # today: none, or all handled
    handled = replace(
        f01,
        expected=replace(f01.expected, deviations={
            "candidate": Deviation("#0", None),
            "reading:Q01": Deviation("#0", None),
            "review:NOME": Deviation("#0", None),
        }),
    )
    check_deviations(handled)
    for key in ("no_results:VID", "reading:Q99"):
        unhandled = replace(
            f01, expected=replace(f01.expected, deviations={key: Deviation("#0", None)})
        )
        with pytest.raises(ValueError, match="the truth sheet does not"):
            check_deviations(unhandled)
