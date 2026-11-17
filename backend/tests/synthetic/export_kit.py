# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Export the test kit for the manual acceptance campaign.

    cd backend
    python -m tests.synthetic.export_kit --out DIR [--database-url URL]

Writes into DIR (which must be empty or not exist) the filled-in submissions
of the kit, one folder per batch of the manual campaign, and the truth sheet
in two CSV files:

    truth_objects.csv       one row per (case, sheet, object)
    truth_submissions.csv   one row per case
    1-kit-pdf/              COLL-MULTI: the PDF cases (all but F22)
    2-kit-images-multi/     COLL-MULTI: G01 (3 PNG), U01–U03
    3-kit-images-single/    COLL-SINGLE: G03 (two uploads), G04 (two files
                            with the same name), G05
    4-kit-single/           COLL-SINGLE: F22, G02

The COLL-* projects are built in the database (default ``TEST_DATABASE_URL``)
inside ONE transaction that is rolled back at the end: nothing is ever
committed. The files are the very bytes the synthetic scenarios upload:
matching recognises a variant by its VID VALUE, so they match any COLL-*
project built from the same spec — the ones ``seed_projects`` creates in a
deployed environment.

The truth sheet comes from the DECLARATIONS only (``SheetFill``,
``SheetOptions``, ``Expected``, ``LooseCase`` and the texts below, from the
test kit), never from running matching or extraction. The "observed" columns
and ``outcome`` are left empty for the operator.
"""
from __future__ import annotations

import argparse
import csv
import io
import math
import os
import random
import re
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

import fitz
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401  (registers every mapper)
from app.core.config import settings
from app.models.enums import MatrixSelectionRule, PageObjectRole, Validity
from tests.synthetic.builders import CollProject, build_coll_multi, build_coll_single
from tests.synthetic.generator import MarkStyle, SheetFill, SheetOptions
from tests.synthetic.runner import loose_files, scenario_sheet
from tests.synthetic.scenarios import (
    LOOSE_CASES,
    SCENARIOS,
    CodeReading,
    LooseCase,
    RespondentIdReading,
    MatrixReading,
    QuestionReading,
    Reading,
    Scenario,
)
from tests.synthetic.specs import (
    CodeSpec,
    FreeTextSpec,
    MatrixSpec,
    QuestionSpec,
    VariantSpec,
)

# ---------------------------------------------------------------------------
# Layout of the kit
# ---------------------------------------------------------------------------

BATCH_PDF = "1-kit-pdf"
BATCH_IMAGES_MULTI = "2-kit-images-multi"
BATCH_IMAGES_SINGLE = "3-kit-images-single"
BATCH_SINGLE = "4-kit-single"

OBJECTS_CSV = "truth_objects.csv"
SUBMISSIONS_CSV = "truth_submissions.csv"

OBJECTS_COLUMNS = (
    "case", "variant", "page", "sheet", "object", "marked_on_paper",
    "expected_reading", "expected_review", "expected_reason",
    "observed_reading", "observed_review", "observed_reason", "outcome", "notes",
)
SUBMISSIONS_COLUMNS = (
    "case", "project", "batch", "variant",
    "status_after_upload", "expected_status", "expected_candidate",
    "expected_missing_pages", "expected_in_review", "expected_points", "expected_max",
    "observed_status_after_upload", "observed_status", "observed_candidate",
    "observed_missing_pages", "observed_in_review", "observed_points",
    "observed_max", "outcome", "notes",
)


def batch_of(scenario: Scenario) -> str:
    if scenario.project == "MULTI":
        return BATCH_IMAGES_MULTI if scenario.image_names else BATCH_PDF
    return BATCH_SINGLE


# What the manual campaign expects beyond the single pass of a scenario, for
# the "notes" column of the case.
CASE_NOTES: dict[str, str] = {
    "F07": "The outcome varies with the threshold.",
    "F18": (
        "With variant A assigned by hand it moves to MATCHED; after a new "
        "Run extraction SCANNED, 28 / 28. Assigning a DRAFT variant (D) is "
        "refused."
    ),
    "F20": (
        "The reason in the queue shows the value read, the one of D. Marked "
        "unresolvable it moves to UNRESOLVABLE and leaves \"To assign\"; "
        "undone it goes back to MATCH_FAILED."
    ),
}


# ---------------------------------------------------------------------------
# Upload errors U01–U03
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UploadErrorCase:
    id: str
    names: tuple[str, ...]
    expected: str  # from the test kit, "loose images and wrong uploads"


U01 = UploadErrorCase(
    "U01", ("U01_report.docx",), "The file ignored and reported, the others uploaded"
)
U02 = UploadErrorCase(
    "U02", ("U02_empty.pdf", "U02_corrupt.pdf"),
    "Refused with a message, the other files go ahead",
)
U03 = UploadErrorCase(
    "U03", ("U03_too-large.pdf",), "413 with a message about the limit"
)
UPLOAD_ERROR_CASES = (U01, U02, U03)

OVERSIZE_MARGIN = 1024 * 1024  # U03: this much over the PDF limit


def docx_bytes() -> bytes:
    """A minimal .docx (a zip with two parts), as in test_upload_checks."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<document/>")
    return out.getvalue()


def corrupt_pdf_bytes() -> bytes:
    """A text file renamed .pdf (U02)."""
    return b"This file is not a PDF: it is text renamed to .pdf.\n"


def oversize_pdf_bytes(limit: int) -> bytes:
    """A VALID PDF over *limit*: one page holding a seeded noise image, which
    no compression shrinks. Same bytes at every run."""
    side = math.isqrt(limit + OVERSIZE_MARGIN) + 1
    noise = Image.frombytes("L", (side, side), random.Random("U03").randbytes(side * side))
    png = io.BytesIO()
    noise.save(png, format="PNG")
    doc = fitz.open()
    try:
        page = doc.new_page(width=595, height=842)
        page.insert_image(page.rect, stream=png.getvalue())
        doc.set_metadata({})
        data = doc.tobytes(garbage=3, deflate=True, no_new_id=True)
    finally:
        doc.close()
    if len(data) <= limit:
        raise RuntimeError(f"U03: {len(data)} bytes, not over the limit of {limit}")
    return data


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


@dataclass
class KitSummary:
    files: list[str]          # relative paths, in writing order
    object_rows: int
    submission_rows: int


def export_kit(db: Session, out: Path) -> KitSummary:
    """Build COLL-MULTI and COLL-SINGLE in *db* and write the kit into *out*.

    The caller owns the transaction (and rolls it back)."""
    projects = {"MULTI": build_coll_multi(db), "SINGLE": build_coll_single(db)}
    files: list[str] = []

    def write(relative: str, data: bytes) -> None:
        path = out / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        files.append(relative)

    object_rows: list[dict[str, str]] = []
    submission_rows: list[dict[str, str]] = []

    for scenario in sorted(SCENARIOS.values(), key=lambda s: case_order(s.id)):
        check_deviations(scenario)
        project = projects[scenario.project]
        batch = batch_of(scenario)
        sheet = scenario_sheet(db, project, scenario)
        if scenario.image_names:
            for name, data in sheet.to_images(scenario.image_names):
                write(f"{batch}/{name}", data)
        else:
            write(f"{batch}/{scenario.id}.pdf", sheet.to_pdf())
        object_rows.extend(object_rows_of(scenario, project))
        submission_rows.append(submission_row_of(scenario, project, batch))

    for case in LOOSE_CASES.values():
        project = projects[case.project]
        for f, (name, data) in loose_files(db, project, case):
            write(f"{BATCH_IMAGES_SINGLE}/{loose_folder(case, f)}/{name}", data)
        submission_rows.append(loose_row_of(case, project))

    contents = {
        "U01_report.docx": docx_bytes(),
        "U02_empty.pdf": b"",
        "U02_corrupt.pdf": corrupt_pdf_bytes(),
        "U03_too-large.pdf": oversize_pdf_bytes(settings.max_submission_pdf_bytes),
    }
    for case in UPLOAD_ERROR_CASES:
        for name in case.names:
            write(f"{BATCH_IMAGES_MULTI}/{name}", contents[name])
        submission_rows.append(
            _submission_row(
                case.id, projects["MULTI"].code, BATCH_IMAGES_MULTI, notes=case.expected
            )
        )

    submission_rows.sort(key=lambda row: case_order(row["case"]))
    write(OBJECTS_CSV, _csv(OBJECTS_COLUMNS, object_rows))
    write(SUBMISSIONS_CSV, _csv(SUBMISSIONS_COLUMNS, submission_rows))
    return KitSummary(files, len(object_rows), len(submission_rows))


def case_order(case_id: str) -> tuple[str, int, str]:
    """Kit order: F01, F02, …, F06, F06b, …, F19, F19b, …, G01, U01."""
    match = re.fullmatch(r"([A-Z])(\d+)(.*)", case_id)
    if match is None:
        raise ValueError(f"unexpected case id {case_id!r}")
    return match[1], int(match[2]), match[3]


def loose_folder(case: LooseCase, f) -> str:
    """G03: one subfolder per upload call; G04: one per file, since the names
    are the same; otherwise the case's own folder."""
    if case.uploads > 1:
        return f"{case.id}/upload-{f.upload}"
    names = [x.name for x in case.files]
    if len(set(names)) < len(names):
        return f"{case.id}/{'abcdefghij'[case.files.index(f)]}"
    return case.id


def _csv(columns: tuple[str, ...], rows: list[dict[str, str]]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in columns})
    # UTF-8 with BOM, so a spreadsheet reads the accents.
    return buf.getvalue().encode("utf-8-sig")


# ---------------------------------------------------------------------------
# Truth sheet — submissions
# ---------------------------------------------------------------------------


def _submission_row(case: str, project: str, batch: str, **fields: str) -> dict[str, str]:
    return {"case": case, "project": project, "batch": batch, **fields}


def _num(value: float) -> str:
    return f"{value:g}"


def submission_row_of(scenario: Scenario, project: CollProject, batch: str) -> dict[str, str]:
    e = scenario.expected
    notes = [CASE_NOTES[scenario.id]] if scenario.id in CASE_NOTES else []
    notes += [
        _deviation_note(key, d) for key, d in e.deviations.items()
        if key in ("candidate", "score")
    ]
    return _submission_row(
        scenario.id, project.code, batch,
        variant=e.variant if e.variant is not None else f"none (printed as {scenario.variant})",
        status_after_upload=e.status_after_upload.value,
        expected_status=e.status.value,
        expected_candidate=e.candidate if e.candidate is not None else "none",
        expected_missing_pages=" ".join(str(p) for p in e.missing_pages),
        expected_in_review=str(len(e.review)),
        expected_points=_num(e.score.points) if e.score is not None else "no score",
        expected_max=_num(e.score.max) if e.score is not None else "",
        notes=" ".join(notes),
    )


def loose_row_of(case: LooseCase, project: CollProject) -> dict[str, str]:
    return _submission_row(
        case.id, project.code, BATCH_IMAGES_SINGLE,
        status_after_upload=case.status_after_upload.value,
        expected_missing_pages=(
            " ".join(str(p) for p in case.missing_pages)
            if case.missing_pages is not None else ""
        ),
        notes=case.expected,
    )


def check_deviations(scenario: Scenario) -> None:
    """Every declared deviation must land in the truth sheet: on the case
    (``candidate``, ``score``) or on an object row (``reading:``/``review:``
    of a key that has a row). Any other key (``no_results:…``) would vanish
    silently, so the export stops instead."""
    e = scenario.expected
    row_keys = {*e.readings, *e.free_texts, *e.review}
    for key in e.deviations:
        kind, _, target = key.partition(":")
        if key in ("candidate", "score"):
            continue
        if kind in ("reading", "review") and target in row_keys:
            continue
        raise ValueError(
            f"{scenario.id}: deviation on {key!r}, which the truth sheet does not "
            "report: add its handling to export_kit"
        )


def _deviation_note(key: str, deviation) -> str:
    return f"Known deviation {deviation.issue} on {key}: observed {deviation.observed!r}."


# ---------------------------------------------------------------------------
# Truth sheet — objects
# ---------------------------------------------------------------------------

STYLE_NAMES = {
    MarkStyle.FULL: "full",
    MarkStyle.LIGHT: "light",
    MarkStyle.HALF: "half",
    MarkStyle.CROSS: "cross",
    MarkStyle.RING: "ring",
}


@dataclass(frozen=True)
class _Sheet:
    page: int  # template page
    fill: SheetFill
    options: SheetOptions


def sheets_of(scenario: Scenario, variant: VariantSpec) -> list[_Sheet]:
    """The submission's sheets in physical order, extra sheets included."""
    pages = scenario.options.pages or tuple(range(1, variant.page_count + 1))
    sheets = [_Sheet(p, scenario.fill, scenario.options) for p in pages]
    sheets += [_Sheet(x.page, x.fill, x.options) for x in scenario.extra_sheets]
    return sheets


def _parse_key(key: str) -> tuple[str, int | None, int | None]:
    """"LABEL", "LABEL@<page>", "LABEL@<page>#<sheet>" (runner.label_keys)."""
    label, _, rest = key.partition("@")
    if not rest:
        return label, None, None
    page, _, sheet = rest.partition("#")
    return label, int(page), int(sheet) if sheet else None


def object_rows_of(scenario: Scenario, project: CollProject) -> list[dict[str, str]]:
    e = scenario.expected
    variant = project.spec.variant(scenario.variant)
    sheets = sheets_of(scenario, variant)
    order = {o.label: i for i, o in enumerate(variant.objects)}

    keys = list(dict.fromkeys([*e.readings, *e.free_texts, *e.review]))
    rows = []
    for key in keys:
        label, page, sheet_no = _parse_key(key)
        spec = variant.obj(label)
        page = page if page is not None else spec.page
        if sheet_no is None:
            sheet_no = 1 + next(i for i, s in enumerate(sheets) if s.page == page)
        sheet = sheets[sheet_no - 1]
        reasons = e.review.get(key)
        notes = [
            _deviation_note(k, e.deviations[k])
            for k in (f"reading:{key}", f"review:{key}")
            if k in e.deviations
        ]
        if key in e.free_texts:
            reading = "non-empty OCR text"
            notes.append("Free text: real OCR, LOW_CONFIDENCE allowed.")
        else:
            reading = format_reading(e.readings[key], spec)
        rows.append(
            (
                (sheet_no, order[label]),
                {
                    "case": scenario.id,
                    "variant": scenario.variant,
                    "page": str(page),
                    # Loose images: the file that holds the sheet, since the
                    # upload order of a folder is not under control.
                    "sheet": (
                        scenario.image_names[sheet_no - 1]
                        if scenario.image_names else str(sheet_no)
                    ),
                    "object": label,
                    "marked_on_paper": marked(spec, sheet),
                    "expected_reading": reading,
                    "expected_review": "yes" if reasons is not None else "no",
                    "expected_reason": "+".join(sorted(reasons)) if reasons else "",
                    "notes": " ".join(notes),
                },
            )
        )
    return [row for _, row in sorted(rows, key=lambda r: r[0])]


def _cell(r: int, c: int) -> str:
    return f"r{r + 1}c{c + 1}"


def _styled(items: list[tuple[str, MarkStyle]]) -> str:
    return ", ".join(f"{name} {STYLE_NAMES[style]}" for name, style in items)


def marked(spec, sheet: _Sheet) -> str:
    """What was put on the paper for this object, from the fill."""
    fill, label = sheet.fill, spec.label
    if isinstance(spec, QuestionSpec):
        letters = fill.answers.get(label, ())
        if not letters:
            return "empty"
        return _styled([(l, fill.styles.get((label, l), fill.default_style)) for l in letters])
    if isinstance(spec, MatrixSpec):
        cells = sorted(tuple(c) for c in fill.cells.get(label, ()))
        if spec.role is PageObjectRole.RESPONDENT_IDENTIFIER:
            return _respondent_id_marks(cells, spec.cols)
        if not cells:
            return "empty"
        return _styled([(_cell(r, c), fill.styles.get((label, (r, c)), fill.default_style)) for r, c in cells])
    if isinstance(spec, FreeTextSpec):
        text = fill.texts.get(label)
        return f"\"{text}\"" if text else "empty"
    if isinstance(spec, CodeSpec):
        covered = any(
            name == label and sheet.page in pages for name, pages in sheet.options.cover
        )
        return "covered" if covered else "printed"
    raise TypeError(f"unsupported object spec {spec!r}")


def _respondent_id_marks(cells: list[tuple[int, int]], cols: int) -> str:
    """One entry per column, the digit marked: "1 0 0 (0 and 3) 0 9", "_" empty."""
    if not cells:
        return "empty"
    columns = []
    for c in range(cols):
        rows = [str(r) for r, col in cells if col == c]
        columns.append("_" if not rows else rows[0] if len(rows) == 1 else f"({' and '.join(rows)})")
    return " ".join(columns)


def format_reading(reading: Reading, spec) -> str:
    if isinstance(reading, QuestionReading):
        text = ",".join(reading.letters) or "none"
        return text if reading.validity is Validity.VALID else f"{text} ({reading.validity.value})"
    if isinstance(reading, MatrixReading):
        text = "; ".join(_cell(r, c) for r, c in sorted(reading.cells)) or "none"
        if reading.invalid_groups:
            by_column = spec.selection_rule in (
                MatrixSelectionRule.ONE_PER_COLUMN, MatrixSelectionRule.MANY_PER_COLUMN
            )
            group = "column" if by_column else "row"
            invalid = ", ".join(f"{group} {g + 1}" for g in sorted(reading.invalid_groups))
            text += f" (invalid: {invalid})"
        return text
    if isinstance(reading, RespondentIdReading):
        if reading.ambiguous_columns:
            ambiguous = ", ".join(str(c + 1) for c in reading.ambiguous_columns)
            return f"{reading.raw_value} (ambiguous columns: {ambiguous})"
        return reading.raw_value
    if isinstance(reading, CodeReading):
        return reading.decoded_value if reading.decode_successful else "not decoded"
    raise TypeError(f"unsupported reading {reading!r}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def check_out_dir(out: Path) -> None:
    """The kit goes into an empty or new folder: never mixed with old files."""
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise SystemExit(
            f"export_kit: {out} exists and is not an empty folder "
            "(an interrupted export leaves a partial kit: delete the folder, "
            "or choose another one, and run it again)"
        )


def code_version() -> str:
    """The git commit the kit is exported from: the printed layout depends on
    the export code, so the kit must come from the commit deployed where it
    is uploaded."""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown (git not available)"
    return f"{head} (with uncommitted changes)" if dirty else head


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tests.synthetic.export_kit",
        description="Export the test kit (PDFs, images, truth sheet).",
    )
    parser.add_argument("--out", required=True, type=Path, help="output folder, empty or new")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("TEST_DATABASE_URL"),
        help="DB to build the projects in, inside a rolled-back transaction "
             "(default: TEST_DATABASE_URL)",
    )
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("--database-url or TEST_DATABASE_URL is required")
    check_out_dir(args.out)

    engine = create_engine(args.database_url, future=True)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            session = Session(bind=connection, join_transaction_mode="create_savepoint")
            try:
                summary = export_kit(session, args.out)
            finally:
                session.close()
                transaction.rollback()  # never a commit
    finally:
        engine.dispose()

    print(
        f"Kit written to {args.out}: {len(summary.files)} files, "
        f"{summary.object_rows} rows in {OBJECTS_CSV}, "
        f"{summary.submission_rows} in {SUBMISSIONS_CSV}.\n"
        f"Code: commit {code_version()}. Upload it only to an environment "
        "with the same commit deployed."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
