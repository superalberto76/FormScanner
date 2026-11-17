# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Golden set of real scans of the test kit.

The scans in ``scans/`` were printed from COLL-MULTI A of a deployed instance,
filled in by hand following each case's filling instructions and scanned. Here
they run through the same path as the synthetic scenarios — upload, matching,
extraction, review read — on a COLL-MULTI built from the same spec in the test
DB: matching recognises the variant from the VID's value.

This module holds the cases, the geometry fingerprint, one scan through the
pipeline and the table of what was observed, the truth sheet ``truth.json``
and the expectation of each case built from it. The expectations are written
by hand: the observed never becomes an expectation by itself.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path

import fitz
from sqlalchemy.orm import Session

from app.models.enums import PageObjectType, Validity
from app.services import submission_review_service as review_codes
from tests.synthetic.builders import CollProject
from tests.synthetic.generator import export_template
from tests.synthetic.runner import Outcome, describe_reading, review_objects, run_pdf
from tests.synthetic.scenarios import (
    SCENARIOS,
    ExpectedScore,
    QuestionReading,
    RespondentIdReading,
    Scenario,
)
from tests.synthetic.template_geometry import TemplateGeometry

GOLDEN_DIR = Path(__file__).parent
SCANS_DIR = GOLDEN_DIR / "scans"
TRUTH_PATH = GOLDEN_DIR / "truth.json"
# Every scan together within 30 MB; above, recompress or rescan.
MAX_SCANS_BYTES = 30 * 1024 * 1024
PAGES_PER_SCAN = 3  # COLL-MULTI A has 3 pages

# Positions are rounded to this many decimals of a point before hashing: well
# below anything a layout change moves, well above float noise.
_FINGERPRINT_DIGITS = 2


@dataclass(frozen=True)
class GoldenCase:
    id: str
    variant: str = "A"

    @property
    def path(self) -> Path:
        return SCANS_DIR / f"{self.id}.pdf"


# The seven sheets on paper, all COLL-MULTI A.
CASES: tuple[GoldenCase, ...] = (
    GoldenCase("F01"),
    GoldenCase("F07"),
    GoldenCase("F08"),
    GoldenCase("F12"),
    GoldenCase("F13"),
    GoldenCase("F21a"),
    GoldenCase("F21b"),
)
CASES_BY_ID = {c.id: c for c in CASES}

# F01 printed with the four corner marks only and scanned from the ADF — the
# real case of a sheet printed without side marks. Not a golden case: it only
# checks that such a sheet is still aligned on its corners, with the strip
# correction not applied.
NO_SIDE_MARKS_DIR = SCANS_DIR / "no_side_marks"
NO_SIDE_MARKS_SCAN = NO_SIDE_MARKS_DIR / "F01.pdf"


# ---------------------------------------------------------------------------
# Geometry fingerprint
# ---------------------------------------------------------------------------


def page_marks(pdf_bytes: bytes) -> dict[int, list[list[float]]]:
    """Per page, the filled rectangles without a stroke the export draws: the
    alignment marks and the hole of the distinct one, in pt, with their fill
    grey level. Read off the PDF drawing like the rest
    of the fingerprint, not from ``app.utils.alignment_marks``."""
    d = _FINGERPRINT_DIGITS
    marks: dict[int, list[list[float]]] = {}
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for index, page in enumerate(doc):
            found = []
            for drawing in page.get_drawings():
                items = drawing["items"]
                if (
                    drawing.get("fill") is not None
                    and drawing.get("color") is None
                    and items
                    and all(item[0] == "re" for item in items)
                ):
                    rect = drawing["rect"]
                    found.append([round(v, d) for v in (*rect, drawing["fill"][0])])
            marks[index + 1] = sorted(found)
    return marks


def geometry_fingerprint(
    geometry: TemplateGeometry, marks: dict[int, list[list[float]]] | None = None
) -> str:
    """A hash of where the STUDENT export prints every object, bubble and
    answer box, and the alignment marks of each page — read back off the PDF
    drawing (``template_geometry``, ``page_marks``), not from the PDF bytes,
    which may change for metadata that moves nothing."""
    d = _FINGERPRINT_DIGITS

    def r(values) -> list[float]:
        return [round(v, d) for v in values]

    pages = []
    for number, page in sorted(geometry.pages.items()):
        objects = []
        for label, obj in sorted(page.objects.items()):
            objects.append({
                "label": label,
                "type": obj.object_type.value,
                "rect": r(obj.rect),
                "bubbles": sorted(
                    [str(key), *r((b.cx, b.cy, b.r))] for key, b in obj.bubbles.items()
                ),
                "text_box": r(obj.text_box) if obj.text_box else None,
            })
        pages.append({
            "page": number,
            "size": r((page.width, page.height)),
            "objects": objects,
            "marks": (marks or {}).get(number, []),
        })
    canonical = json.dumps(pages, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def project_fingerprint(db: Session, project: CollProject, variant_key: str = "A") -> str:
    """The fingerprint of *project*'s variant as the current code exports it."""
    pdf, geometry = export_template(db, project, variant_key)
    return geometry_fingerprint(geometry, page_marks(pdf))


# ---------------------------------------------------------------------------
# Truth sheet and expectations
# ---------------------------------------------------------------------------

TRUTH_FORMAT = 2
REQUIREMENT = "requirement"  # read exactly as written, checked strictly
KNOWN_LIMIT = "known_limit"  # only reported, but must be in review
REPRINT_MESSAGE = "golden set printed with another layout: reprint it"
# The flag reasons a question can show in review (the app's codes, not
# copies): a typo in truth.json is reported as such, not as a check error.
QUESTION_REVIEW_REASONS = frozenset({
    review_codes.LOW_CONFIDENCE,
    review_codes.INVALID_ANSWER,
    review_codes.SHEET_NOT_ALIGNED,
    review_codes.EXTRACTION_FAILED,
})


@dataclass(frozen=True)
class TruthObject:
    category: str
    note: str
    # Only for a requirement, on a QUESTION (the only kind the filling
    # instructions change today): the reading and the exact review reasons
    # (empty = not in review).
    reading: QuestionReading | None = None
    review: frozenset[str] = frozenset()


@dataclass(frozen=True)
class TruthCase:
    id: str
    file: str
    print_info: dict  # optional printing notes (e.g. the scale)
    scan_info: dict
    respondent_id: str
    score: ExpectedScore
    objects: dict[str, TruthObject]


@dataclass(frozen=True)
class Truth:
    base: str  # the synthetic scenario every case starts from
    project: str
    variant: str
    fingerprint: str
    cases: dict[str, TruthCase]


def load_truth(path: Path = TRUTH_PATH) -> Truth:
    """``truth.json`` as written by hand; a malformed entry raises."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw["format"] != TRUTH_FORMAT:
        raise ValueError(f"truth.json: format {raw['format']!r}, expected {TRUTH_FORMAT}")
    return Truth(
        base=raw["base"],
        project=raw["project"],
        variant=raw["variant"],
        fingerprint=raw["fingerprint"],
        cases={
            case_id: _truth_case(case_id, entry)
            for case_id, entry in raw["cases"].items()
        },
    )


def _truth_case(case_id: str, entry: dict) -> TruthCase:
    return TruthCase(
        id=case_id,
        file=entry["file"],
        print_info=entry.get("print", {}),
        scan_info=entry["scan"],
        respondent_id=entry["respondent_id"],
        score=ExpectedScore(
            float(entry["score"]["points"]), float(entry["score"]["max"])
        ),
        objects={
            label: _truth_object(f"{case_id} {label}", obj)
            for label, obj in entry["objects"].items()
        },
    )


def _truth_object(where: str, obj: dict) -> TruthObject:
    category = obj["category"]
    if category == KNOWN_LIMIT:
        if "reading" in obj or "review" in obj:
            raise ValueError(f"truth.json {where}: a known limit has no reading nor review")
        return TruthObject(category, obj["note"])
    if category != REQUIREMENT:
        raise ValueError(f"truth.json {where}: category {category!r}")
    reading = obj["reading"]
    return TruthObject(
        category,
        obj["note"],
        reading=QuestionReading(
            letters=tuple(sorted(reading["letters"])),
            validity=Validity(reading["validity"]),
        ),
        review=frozenset(obj["review"]),
    )


def case_expectation(truth: Truth, case_id: str) -> tuple[Scenario, frozenset[str]]:
    """The expectation of *case_id* and its known limits.

    The base scenario's expectation (F01) with the case's respondent ID,
    candidate and score; a requirement replaces the reading and review of its object; a
    known limit leaves the exact checks and goes to ``check(loose=...)``.
    """
    entry = truth.cases[case_id]
    base = SCENARIOS[truth.base]
    expected = base.expected
    readings = {**expected.readings, "MATRICOLA": RespondentIdReading(entry.respondent_id)}
    review = dict(expected.review)
    free_texts = list(expected.free_texts)
    loose: set[str] = set()
    for label, obj in entry.objects.items():
        if obj.category == KNOWN_LIMIT:
            loose.add(label)
            readings.pop(label, None)
            review.pop(label, None)
            if label in free_texts:
                free_texts.remove(label)
            continue
        readings[label] = obj.reading
        if obj.review:
            review[label] = obj.review
        else:
            review.pop(label, None)
    scenario = replace(
        base,
        id=case_id,
        description=f"{case_id} — golden set, expectations of {truth.base} with the differences in truth.json",
        expected=replace(
            expected,
            candidate=entry.respondent_id,
            readings=readings,
            review=review,
            free_texts=tuple(free_texts),
            score=entry.score,
        ),
    )
    return scenario, frozenset(loose)


def truth_problems(truth: Truth, project: CollProject) -> list[str]:
    """What makes ``truth.json`` inconsistent with the cases and COLL-MULTI."""
    problems: list[str] = []
    if sorted(truth.cases) != sorted(CASES_BY_ID):
        problems.append(f"cases: {sorted(truth.cases)}, expected {sorted(CASES_BY_ID)}")
    if truth.base not in SCENARIOS:
        problems.append(f"base {truth.base!r}: no scenario with this id")
    if truth.project != project.code:
        problems.append(f"project {truth.project!r}, built {project.code!r}")
    variant = project.variants.get(truth.variant)
    if variant is None:
        return problems + [f"variant {truth.variant!r} missing in {project.code}"]
    base_max = SCENARIOS[truth.base].expected.score.max if truth.base in SCENARIOS else None
    for case_id, entry in truth.cases.items():
        case = CASES_BY_ID.get(case_id)
        if case is not None and entry.file != case.path.name:
            problems.append(f"{case_id}: file {entry.file!r}, expected {case.path.name!r}")
        if base_max is not None and entry.score.max != base_max:
            problems.append(f"{case_id}: max {entry.score.max:g}, that of {truth.base} is {base_max:g}")
        if not 0 <= entry.score.points <= entry.score.max:
            problems.append(f"{case_id}: points {entry.score.points:g} outside 0..{entry.score.max:g}")
        for label, obj in entry.objects.items():
            refs = variant.objs(label)
            if len(refs) != 1:
                problems.append(f"{case_id} {label}: {len(refs)} objects in the variant")
                continue
            ref = refs[0]
            if obj.category != REQUIREMENT:
                continue
            if ref.object_type is not PageObjectType.QUESTION:
                problems.append(
                    f"{case_id} {label}: requirement on {ref.object_type.value}, "
                    "only questions are supported"
                )
                continue
            letters = {o.letter for o in ref.options.values()}
            unknown = set(obj.reading.letters) - letters
            if unknown:
                problems.append(f"{case_id} {label}: letters {sorted(unknown)} not printed")
            bad_reasons = obj.review - QUESTION_REVIEW_REASONS
            if bad_reasons:
                problems.append(f"{case_id} {label}: unknown review reasons {sorted(bad_reasons)}")
    return problems


# ---------------------------------------------------------------------------
# Run and observe
# ---------------------------------------------------------------------------


def run_case(db: Session, project: CollProject, case: GoldenCase) -> Outcome:
    return run_pdf(db, project, case.id, case.path.read_bytes())


@dataclass(frozen=True)
class ObservedRow:
    key: str  # as the runner keys results: label, or label@page for the VID copies
    page: int
    result_type: str
    reading: str
    validity: str
    confidence: str
    review: str  # reasons, or "" when the object is not in review


def observed_rows(outcome: Outcome, project: CollProject, variant_key: str = "A") -> list[ObservedRow]:
    """One row per result of the submission, in page order."""
    variant = project.variants[variant_key]
    listed = review_objects(outcome.detail)
    rows = []
    for key, result in outcome.results.items():
        listed_obj = listed.get(key)
        payload = result.detected_payload_json or {}
        rows.append(ObservedRow(
            key=key,
            page=result.submission_page.form_variant_page.page_index,
            result_type=result.result_type.value,
            reading=describe_reading(result, variant),
            validity=payload.get("validity") or "",
            confidence=(
                f"{result.confidence_score:.3f}" if result.confidence_score is not None else "—"
            ),
            review=", ".join(listed_obj.flag_reasons) if listed_obj is not None else "",
        ))
    return sorted(rows, key=lambda row: (row.page, _label_order(row.key)))


def _label_order(key: str) -> tuple[int, str]:
    # The VID first, then the labels as printed (MATRICOLA, NOME, Q01…).
    return (0 if key.startswith("VID") else 1, key)


def render(
    case: GoldenCase,
    outcome: Outcome,
    rows: list[ObservedRow],
    errors: list[str] | None,
    notes: list[str] | None = None,
) -> str:
    """The observed table of one case, as plain text; with *errors*, the
    comparison with its expectation from ``truth.json``."""
    detail = outcome.detail
    score = detail.score
    score_text = (
        "—" if score is None
        else f"{score.raw_score:g}/{score.max_possible_score:g}"
    )
    lines = [
        f"== {case.id} ({case.path.name}) ==",
        f"status after upload: {outcome.status_after_upload.value}, final: {detail.status.value}, "
        f"candidate: {detail.respondent_identifier!r}, missing pages: {detail.missing_page_indices}, "
        f"score: {score_text}",
        "sheets: " + ", ".join(
            f"{page}:{source.value}:{confidence}" for page, source, confidence in outcome.pages
        ),
        f"extraction: {outcome.summary.errors} errors",
    ]
    header = ("page", "object", "type", "reading", "validity", "conf", "review")
    table = [header] + [
        (str(r.page), r.key, r.result_type, r.reading, r.validity, r.confidence, r.review)
        for r in rows
    ]
    widths = [max(len(row[i]) for row in table) for i in range(len(header))]
    for row in table:
        lines.append("  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip())
    if errors is not None:
        lines.append(f"comparison with truth.json: {len(errors)} discrepancies")
        lines.extend(f"  - {e}" for e in errors)
        lines.extend(f"  note: {n}" for n in notes or ())
    return "\n".join(lines)
