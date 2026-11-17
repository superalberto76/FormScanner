# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run a scenario on the app's real path and compare it with its expectations.

``run_scenario`` follows the path an operator's upload takes — generate →
create_batch → upload_pdf (matching included) → run_batch_extraction → the
review read — and records what it observed at each step, the matching outcome
right after the upload, before extraction moves the status on.

``check`` compares an ``Outcome`` with the scenario's ``Expected`` and returns
EVERY discrepancy as a readable message, never stopping at the first one; the
score of the submission too.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, replace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import (
    PageOrderSource,
    SubmissionResultType,
    SubmissionStatus,
    Validity,
)
from app.models.submission_object_result import SubmissionObjectResult
from app.models.submission_page import SubmissionPage
from app.models.user import User
from app.schemas.submission import (
    ExtractionRunSummary,
    SubmissionBatchCreate,
    SubmissionReviewDetail,
    SubmissionReviewObject,
)
from app.services import submission_batch_service, submission_review_service
from tests.synthetic.builders import CollProject, CollVariant
from tests.synthetic.generator import GeneratedSheet, SheetOptions, digits, generate
from tests.synthetic.scenarios import (
    ALWAYS_REVIEW,
    CLEAN_SINGLE,
    CodeReading,
    ExpectedScore,
    LooseCase,
    LooseFile,
    RespondentIdReading,
    MatrixReading,
    QuestionReading,
    Reading,
    Scenario,
)

LOW_CONFIDENCE = submission_review_service.LOW_CONFIDENCE


@dataclass
class Outcome:
    scenario_id: str
    submission_id: uuid.UUID
    status_after_upload: SubmissionStatus
    variant_id: uuid.UUID | None
    # Per sheet, physical order: (template page, order source, match_confidence).
    pages: tuple[tuple[int | None, PageOrderSource, float | None], ...]
    summary: ExtractionRunSummary
    detail: SubmissionReviewDetail
    results: dict[str, SubmissionObjectResult]  # by label, every result of the submission
    timings: dict[str, float]  # generate, upload, extract, read

    def review_objects(self) -> dict[str, SubmissionReviewObject]:
        return review_objects(self.detail)


def review_objects(detail: SubmissionReviewDetail) -> dict[str, SubmissionReviewObject]:
    """Listed objects by label, keyed like the results (``label_keys``)."""
    listed = [
        (
            o.label,
            p.template_page.page_index if p.template_page else None,
            p.source_page_order + 1,
            o,
        )
        for p in detail.pages
        for o in p.objects
    ]
    return label_keys(listed)


def label_keys(items: list[tuple[str, int | None, int, object]]) -> dict[str, object]:
    """``(label, template page, sheet, value)`` → value by check key.

    A label read once is keyed by itself; a label read on several sheets (the
    VID copies replicated onto every page) "LABEL@<template page>"; and when
    two sheets sit on the same template page (a duplicate sheet, F19)
    "LABEL@<template page>#<sheet>", the sheet being the physical position
    from 1.
    """
    per_label: dict[str, int] = {}
    per_page: dict[tuple[str, int | None], int] = {}
    for label, page, _sheet, _value in items:
        per_label[label] = per_label.get(label, 0) + 1
        per_page[(label, page)] = per_page.get((label, page), 0) + 1
    keyed: dict[str, object] = {}
    for label, page, sheet, value in items:
        if per_label[label] == 1:
            key = label
        elif per_page[(label, page)] == 1:
            key = f"{label}@{page}"
        else:
            key = f"{label}@{page}#{sheet}"
        keyed[key] = value
    return keyed


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def scenario_sheet(db: Session, project: CollProject, scenario: Scenario) -> GeneratedSheet:
    """The scenario's submission as generated sheets, extra sheets included."""
    options = scenario.options
    if scenario.extra_sheets:
        extra = tuple(
            generate(
                db, project, e.variant, e.fill, replace(e.options, pages=(e.page,))
            ).page_images[0]
            for e in scenario.extra_sheets
        )
        options = replace(options, extra_pages=options.extra_pages + extra)
    return generate(db, project, scenario.variant, scenario.fill, options)


def scenario_pdf(db: Session, project: CollProject, scenario: Scenario) -> bytes:
    """The scenario's submission as the PDF an operator would upload."""
    return scenario_sheet(db, project, scenario).to_pdf()


def loose_files(
    db: Session, project: CollProject, case: LooseCase
) -> list[tuple[LooseFile, tuple[str, bytes]]]:
    """Each file of a loose case (G03–G05) with its ``(name, bytes)``, as
    ``upload_images`` takes them: one template page of a clean COLL-SINGLE
    submission each. A page's pixels depend only on seed, page and fill, so a
    file generated alone equals the same page generated with the others."""
    out = []
    for f in case.files:
        fill = replace(CLEAN_SINGLE, cells={"MATRICOLA": digits(f.respondent_id)})
        sheet = generate(db, project, "A", fill, SheetOptions(seed=f.seed, pages=(f.page,)))
        out.append((f, sheet.to_images((f.name,))[0]))
    return out


def upload_scenarios(db: Session, project: CollProject, name: str, scenarios):
    """One batch *name* with each scenario's PDF uploaded by the owner, for the
    multi-step flows: ``(owner, batch, {scenario id: submission})``. Not run."""
    owner = db.get(User, project.owner_id)
    batch = submission_batch_service.create_batch(
        db, project.project_id, SubmissionBatchCreate(name=name), owner
    )
    submissions = {
        s.id: submission_batch_service.upload_pdf(
            db, batch.id, f"{s.id}.pdf", scenario_pdf(db, project, s), owner
        )
        for s in scenarios
    }
    return owner, batch, submissions


@dataclass(frozen=True)
class QueueState:
    """One submission as the batch queue, the review page and the project's
    batch list show it. The FE buckets are built from these."""
    status: SubmissionStatus
    pending: int  # the queue's pending_review_count
    review_pending: int  # the review page's pending_review_count
    batch_pending: int  # the batch's, in the project's batch list (non-closed submissions)
    lock: str | None  # display name of the active lock in the queue
    review_lock: str | None  # ... and on the review page
    candidate: str | None


def queue_state(db: Session, batch, submission, user: User) -> QueueState:
    queued = next(
        (
            s for s in submission_batch_service.get_batch_detail(db, batch.id, user).submissions
            if s.id == submission.id
        ),
        None,
    )
    assert queued is not None, f"submission {submission.id} not queued in batch {batch.id}"
    detail = submission_review_service.get_review_detail(db, submission.id, user)
    listed = next(
        (
            b for b in submission_batch_service.list_batches_for_project(
                db, detail.form_project_id, user
            )
            if b.id == batch.id
        ),
        None,
    )
    assert listed is not None, f"batch {batch.id} not in the project's list"
    return QueueState(
        status=queued.status,
        pending=queued.pending_review_count,
        review_pending=detail.pending_review_count,
        batch_pending=listed.pending_review_count,
        lock=queued.lock.display_name if queued.lock else None,
        review_lock=detail.lock.display_name if detail.lock else None,
        candidate=queued.respondent_identifier,
    )


@dataclass(frozen=True)
class Matching:
    """Matching's outcome for one submission, recorded right after the upload,
    before extraction moves the status on."""
    status: SubmissionStatus
    variant_id: uuid.UUID | None
    pages: tuple[tuple[int | None, PageOrderSource, float | None], ...]


def matching_of(submission) -> Matching:
    return Matching(
        status=submission.status,
        variant_id=submission.matched_form_variant_id,
        pages=tuple(
            (
                p.form_variant_page.page_index if p.form_variant_page is not None else None,
                p.page_order_source,
                p.match_confidence,
            )
            for p in sorted(submission.pages, key=lambda p: p.source_page_order)
        ),
    )


def run_scenario(db: Session, project: CollProject, scenario: Scenario) -> Outcome:
    start = time.perf_counter()
    pdf = scenario_pdf(db, project, scenario)
    generated = time.perf_counter() - start
    return run_pdf(db, project, scenario.id, pdf, {"generate": generated})


def run_pdf(
    db: Session,
    project: CollProject,
    case_id: str,
    pdf: bytes,
    timings: dict[str, float] | None = None,
) -> Outcome:
    """*pdf* through the same path as a scenario: a batch *case_id*, the upload
    (matching included), the extraction run and the review read. The PDF can
    be a generated one or a real scan (golden set)."""
    owner = db.get(User, project.owner_id)
    timings = dict(timings) if timings else {}

    start = time.perf_counter()
    batch = submission_batch_service.create_batch(
        db, project.project_id, SubmissionBatchCreate(name=case_id), owner
    )
    submission = submission_batch_service.upload_pdf(
        db, batch.id, f"{case_id}.pdf", pdf, owner
    )
    timings["upload"] = time.perf_counter() - start
    matching = matching_of(submission)

    start = time.perf_counter()
    summary = submission_batch_service.run_batch_extraction(db, batch.id, owner)
    timings["extract"] = time.perf_counter() - start

    return read_outcome(db, case_id, submission, matching, summary, owner, timings)


def read_outcome(
    db: Session,
    scenario_id: str,
    submission,
    matching: Matching,
    summary: ExtractionRunSummary,
    owner: User,
    timings: dict[str, float],
) -> Outcome:
    """The review read and every result of *submission*, after a run."""
    start = time.perf_counter()
    db.refresh(submission)
    detail = submission_review_service.get_review_detail(db, submission.id, owner)
    timings["read"] = time.perf_counter() - start

    rows = db.scalars(
        select(SubmissionObjectResult)
        .join(SubmissionPage)
        .where(SubmissionPage.submission_id == submission.id)
    ).all()
    results = label_keys(
        [
            (
                r.page_object.label,
                r.submission_page.form_variant_page.page_index,
                r.submission_page.source_page_order + 1,
                r,
            )
            for r in rows
        ]
    )

    return Outcome(
        scenario_id=scenario_id,
        submission_id=submission.id,
        status_after_upload=matching.status,
        variant_id=matching.variant_id,
        pages=matching.pages,
        summary=summary,
        detail=detail,
        results=results,
        timings=timings,
    )


def report(outcome: Outcome, notes: list[str]) -> None:
    """The compact report a scenario test prints (``pytest -s``)."""
    t = outcome.timings
    lines = [
        f"\n[synthetic] {outcome.scenario_id}: "
        + " ".join(
            f"{phase} {t[phase]:.2f}s"
            for phase in ("generate", "upload", "extract", "read")
            if phase in t  # no "generate" for a real scan (run_pdf)
        )
        + f" (total {sum(t.values()):.2f}s)",
        f"  status after upload: {outcome.status_after_upload.value}, "
        f"final: {outcome.detail.status.value}, candidate: {outcome.detail.respondent_identifier!r}",
        "  sheets: " + ", ".join(
            f"{page}:{source.value}:{confidence}" for page, source, confidence in outcome.pages
        ),
    ]
    for key, obj in outcome.review_objects().items():
        conf = f"{obj.confidence_score:.3f}" if obj.confidence_score is not None else "—"
        lines.append(f"  review {key}: {', '.join(obj.flag_reasons)} (conf {conf})")
    for key, result in outcome.results.items():
        if result.page_object.label not in ("MATRICOLA", "NOME", "APERTA"):
            continue
        conf = f"{result.confidence_score:.3f}" if result.confidence_score is not None else "—"
        payload = result.detected_payload_json or {}
        read = payload.get("raw_value", payload.get("suggested_text"))
        lines.append(f"  {key}: conf {conf}, read {read!r}")
    lines.append(f"  score: {_score_text(outcome.detail)}")
    lines.append(f"  known deviations still present: {len(notes)}")
    lines.extend(f"    - {note}" for note in notes)
    print("\n".join(lines))


# ---------------------------------------------------------------------------
# Check
# ---------------------------------------------------------------------------


def check(
    outcome: Outcome,
    project: CollProject,
    scenario: Scenario,
    notes: list[str] | None = None,
    loose: frozenset[str] = frozenset(),
) -> list[str]:
    """Every discrepancy between *outcome* and the scenario's expectations,
    as messages.

    Known deviations still present are not errors; they are appended to
    *notes*, when given, for the report.

    *loose* (golden set): result keys of known limits. Their reading and
    review reasons are not compared and they are not checked as free texts;
    they must only be in review — a known limit read without review is an
    app bug, or a correct reading to reclassify. A free text among them must
    still carry ALWAYS_REVIEW. Their reading and reasons go to *notes*.
    The scenarios never pass it.
    """
    expected = scenario.expected
    deviations = expected.deviations
    errors: list[str] = []
    notes = notes if notes is not None else []
    overlap = loose & (
        set(expected.readings)
        | set(expected.review)
        | set(expected.free_texts)
        | {key.split(":", 1)[1] for key in expected.deviations}
    )
    if overlap:
        raise ValueError(f"loose labels also expected exactly: {sorted(overlap)}")
    variant = project.variants[expected.variant] if expected.variant is not None else None
    detail = outcome.detail

    def compare(key: str, want: object, got: object) -> None:
        deviation = deviations.get(key)
        if deviation is None:
            if got != want:
                errors.append(f"{key}: expected {want!r}, observed {got!r}")
        elif got == deviation.observed:
            notes.append(f"known deviation {deviation.issue} still present on {key}")
        elif got == want:
            errors.append(
                f"deviation {deviation.issue} resolved on {key}: remove it from the scenario"
            )
        else:
            errors.append(
                f"{key}: expected {want!r}, deviation {deviation.issue} "
                f"{deviation.observed!r}, observed {got!r}"
            )

    # 1. matching
    if outcome.status_after_upload is not expected.status_after_upload:
        errors.append(
            f"status after upload: expected {expected.status_after_upload.value}, "
            f"observed {outcome.status_after_upload.value}"
        )
    expected_variant_id = variant.variant_id if variant is not None else None
    if outcome.variant_id != expected_variant_id:
        errors.append(
            f"variant: expected {expected.variant} ({expected_variant_id}), "
            f"observed {outcome.variant_id}"
        )

    # 2. sheets
    if outcome.pages != expected.pages:
        errors.append(f"pages: expected {_pages(expected.pages)}, observed {_pages(outcome.pages)}")

    # 3. extraction
    if outcome.summary.errors != 0:
        errors.append(
            f"extraction: {outcome.summary.errors} errors: {outcome.summary.error_details}"
        )
    if detail.status is not expected.status:
        errors.append(
            f"final status: expected {expected.status.value}, observed {detail.status.value}"
        )

    # 4. candidate
    compare("candidate", expected.candidate, detail.respondent_identifier)

    # 5. missing pages
    if detail.missing_page_indices != list(expected.missing_pages):
        errors.append(
            f"missing pages: expected {list(expected.missing_pages)}, "
            f"observed {detail.missing_page_indices}"
        )

    # 6. review
    listed = outcome.review_objects()
    observed_reasons = {
        label: _reasons(obj, expected.review.get(label)) for label, obj in listed.items()
    }
    review_keys = {f"review:{label}" for label in listed if label not in loose} | {
        f"review:{label}" for label in expected.review
    }
    for key in sorted(review_keys | {k for k in deviations if k.startswith("review:")}):
        label = key.removeprefix("review:")
        compare(key, expected.review.get(label), observed_reasons.get(label))
    n_listed = sum(len(p.objects) for p in detail.pages)
    if not (detail.review_count == detail.pending_review_count == n_listed):
        errors.append(
            f"inconsistent review counts: review_count={detail.review_count}, "
            f"pending_review_count={detail.pending_review_count}, listed objects={n_listed}"
        )

    # 7. readings
    for label, want in expected.readings.items():
        result = outcome.results.get(label)
        got = _reading(result, want, variant) if result is not None else None
        compare(f"reading:{label}", want, got)
    # Every result is accounted for: a reading the scenario does not expect is
    # an error too (a duplicate sheet read twice, a sheet read on the wrong page).
    unexpected = sorted(
        set(outcome.results)
        - set(expected.readings)
        - set(expected.free_texts)
        - loose
        - {k for k in outcome.results if k.split("@")[0] in expected.no_results}
    )
    if unexpected:
        errors.append(f"unexpected readings: {unexpected}")

    # 7b. labels that must have no result at all, on any sheet
    for label in expected.no_results:
        found = frozenset(
            f"{label}@{r.submission_page.form_variant_page.page_index}"
            for r in outcome.results.values()
            if r.page_object.label == label
        )
        compare(f"no_results:{label}", frozenset(), found)

    # 7c. known limits (golden set): only "in review". The message carries
    # the reading: a correct one out of review is not a bug but a known limit
    # to reclassify as a requirement.
    for label in sorted(loose):
        result = outcome.results.get(label)
        if result is None:
            errors.append(f"known limit {label}: no result")
            continue
        reading = describe_reading(result, variant) if variant is not None else "?"
        obj = listed.get(label)
        if obj is None:
            errors.append(
                f"known limit {label} read without review: a bug, or a correct "
                f"reading to reclassify as a requirement (read {reading})"
            )
            continue
        notes.append(
            f"known limit {label}: read {reading}, in review ({', '.join(obj.flag_reasons)})"
        )
        # a free text is always in review by rule, not by its reading
        if obj.result_type is SubmissionResultType.FREE_TEXT and ALWAYS_REVIEW not in obj.flag_reasons:
            errors.append(
                f"known limit {label}: ALWAYS_REVIEW missing (reasons: {obj.flag_reasons})"
            )

    # 8. free texts
    for label in expected.free_texts:
        result = outcome.results.get(label)
        if result is None:
            errors.append(f"free text {label}: no result")
            continue
        obj = listed.get(label)
        if obj is None or ALWAYS_REVIEW not in obj.flag_reasons:
            errors.append(
                f"free text {label}: ALWAYS_REVIEW missing "
                f"(reasons: {obj.flag_reasons if obj else None})"
            )
        text = (result.detected_payload_json or {}).get("suggested_text") or ""
        if not text.strip():
            errors.append(f"free text {label}: empty OCR suggestion")

    # 9. score: the review read's triple
    compare("score", _expected_score(expected.score), _observed_score(detail))

    return errors


def describe_reading(result: SubmissionObjectResult, variant: CollVariant) -> str:
    """The reading in the terms of the paper: option letters, 1-based cells.
    Used for reports (golden set): the observed table and the message of a
    known limit."""
    payload = result.detected_payload_json
    if payload is None:
        return "(no reading)"
    kind = result.result_type
    if kind is SubmissionResultType.QUESTION:
        letters = {
            str(o.option_id): o.letter
            for o in variant.obj(result.page_object.label).options.values()
        }
        chosen = sorted(letters[o["option_id"]] for o in payload["selected_options"])
        return ",".join(chosen) or "—"
    if kind is SubmissionResultType.MATRIX:
        cells: set[tuple[int, int]] = set()
        invalid: list[str] = []
        for row in payload.get("rows", []):
            cells.update((row["row_index"], c["col_index"]) for c in row["selected_cells"])
            if row["validity"] != "VALID":
                invalid.append(f"row {row['row_index'] + 1} {row['validity']}")
        for col in payload.get("cols", []):
            cells.update((c["row_index"], col["col_index"]) for c in col["selected_cells"])
            if col["validity"] != "VALID":
                invalid.append(f"column {col['col_index'] + 1} {col['validity']}")
        text = " ".join(f"r{r + 1}c{c + 1}" for r, c in sorted(cells)) or "—"
        return f"{text} (invalid: {'; '.join(invalid)})" if invalid else text
    if kind is SubmissionResultType.RESPONDENT_IDENTIFIER:
        # ambiguous_columns are 0-based, as in export_kit.format_reading
        ambiguous = payload.get("ambiguous_columns") or []
        extra = f" (ambiguous columns: {', '.join(str(c + 1) for c in ambiguous)})" if ambiguous else ""
        return f"{payload.get('raw_value')!r}{extra}"
    if kind is SubmissionResultType.CODE:
        if payload.get("decode_successful"):
            return repr(payload.get("decoded_value"))
        return "not decoded"
    if kind is SubmissionResultType.FREE_TEXT:
        text = (payload.get("suggested_text") or "").replace("\n", " ")
        return repr(text if len(text) <= 60 else text[:57] + "...")
    return json.dumps(payload)[:60]


# The percentage is a float computed by the app: compared to 6 decimals.
_PCT_DIGITS = 6


def _expected_score(score: ExpectedScore | None) -> tuple | None:
    """(points, max, percentage), the percentage derived; None = no score."""
    if score is None:
        return None
    pct = score.points / score.max * 100 if score.max else None
    return (score.points, score.max, None if pct is None else round(pct, _PCT_DIGITS))


def _observed_score(detail: SubmissionReviewDetail) -> tuple | None:
    score = detail.score
    if score is None:
        return None
    pct = score.percentage
    return (
        score.raw_score,
        score.max_possible_score,
        None if pct is None else round(pct, _PCT_DIGITS),
    )


def _score_text(detail: SubmissionReviewDetail) -> str:
    score = detail.score
    if score is None:
        return "—"
    pct = "—" if score.percentage is None else f"{score.percentage:.1f}%"
    return f"{score.raw_score:g}/{score.max_possible_score:g} ({pct})"


def _pages(pages) -> str:
    return "[" + ", ".join(
        f"{n}:{source.value}:{confidence}" for n, source, confidence in pages
    ) + "]"


def _reasons(
    obj: SubmissionReviewObject, expected: frozenset[str] | None
) -> frozenset[str]:
    reasons = frozenset(obj.flag_reasons)
    # OCR confidence is informative only: an extra LOW_CONFIDENCE on a free text
    # is tolerated.
    if (
        obj.result_type is SubmissionResultType.FREE_TEXT
        and LOW_CONFIDENCE not in (expected or ())
    ):
        reasons -= {LOW_CONFIDENCE}
    return reasons


def _reading(result: SubmissionObjectResult, want: Reading, variant) -> Reading | None:
    payload = result.detected_payload_json
    if payload is None:
        return None
    if isinstance(want, QuestionReading):
        letters = {
            str(o.option_id): o.letter for o in variant.obj(result.page_object.label).options.values()
        }
        return QuestionReading(
            letters=tuple(sorted(letters[o["option_id"]] for o in payload["selected_options"])),
            validity=Validity(payload["validity"]),
        )
    if isinstance(want, MatrixReading):
        cells: set[tuple[int, int]] = set()
        invalid: set[int] = set()
        for row in payload.get("rows", []):
            cells.update((row["row_index"], c["col_index"]) for c in row["selected_cells"])
            if row["validity"] != Validity.VALID.value:
                invalid.add(row["row_index"])
        for col in payload.get("cols", []):
            cells.update((c["row_index"], col["col_index"]) for c in col["selected_cells"])
            if col["validity"] != Validity.VALID.value:
                invalid.add(col["col_index"])
        return MatrixReading(frozenset(cells), frozenset(invalid))
    if isinstance(want, CodeReading):
        return CodeReading(
            decoded_value=payload["decoded_value"],
            decode_successful=payload["decode_successful"],
        )
    if isinstance(want, RespondentIdReading):
        return RespondentIdReading(
            raw_value=payload["raw_value"],
            ambiguous_columns=tuple(payload.get("ambiguous_columns", [])),
        )
    raise TypeError(f"unsupported expected reading {want!r}")
