# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Extraction flows of the test kit.

* One batch with F01, F05, F07 and F09 — a second run reads nothing new and
  changes nothing; the threshold moved to 0.95 and to 0.5 never raises a
  count above 0.95's nor lowers one below 0.5's, never lets out F05's blank
  questions or F09's ambiguous respondent ID, never touches a reading, and
  the reset brings the counts back.
* A clean submission whose page 2 image disappears before the run — it stays
  MATCHED with page 2's objects unread, and "Review anyway" brings it to
  SCANNED with an EXTRACTION_FAILED placeholder for each.

The counts are not invented: the default ones are the scenarios' declared
review, the others are asserted by direction. Marked ``synthetic``:
``pytest -m synthetic tests/synthetic -s``.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models.enums import SubmissionStatus
from app.models.submission import Submission
from app.models.submission_object_result import SubmissionObjectResult
from app.models.submission_page import SubmissionPage
from app.services import (
    review_anyway_service,
    submission_batch_service,
    submission_review_service,
)
from tests.synthetic.runner import review_objects, upload_scenarios
from tests.synthetic.scenarios import (
    ALWAYS_REVIEW,
    INVALID_ANSWER,
    PAGE_2_LABELS,
    REVIEW_ANYWAY,
    SCENARIOS,
)

pytestmark = [
    pytest.mark.synthetic,
    pytest.mark.skipif(
        shutil.which("tesseract") is None,
        reason="Tesseract not installed: the scenarios use real OCR",
    ),
]

EXTRACTION_FAILED = submission_review_service.EXTRACTION_FAILED
THRESHOLD_IDS = ("F01", "F05", "F07", "F09")


def _state(db, batch, submissions, owner):
    """{scenario id: (status, pending_review_count)} from the batch queue."""
    by_id = {
        s.id: s for s in submission_batch_service.get_batch_detail(db, batch.id, owner).submissions
    }
    return {
        key: (by_id[sub.id].status, by_id[sub.id].pending_review_count)
        for key, sub in submissions.items()
    }


def _in_review(db, submission, owner) -> dict[str, frozenset[str]]:
    detail = submission_review_service.get_review_detail(db, submission.id, owner)
    return {k: frozenset(o.flag_reasons) for k, o in review_objects(detail).items()}


def _result_count(db, batch) -> int:
    return db.scalar(
        select(func.count(SubmissionObjectResult.id))
        .join(SubmissionPage)
        .join(Submission)
        .where(Submission.submission_batch_id == batch.id)
    )


def _readings(db, batch) -> dict:
    """What the extractor wrote for each result: the threshold must not touch it."""
    rows = db.execute(
        select(
            SubmissionObjectResult.id,
            SubmissionObjectResult.detected_payload_json,
            SubmissionObjectResult.confidence_score,
            SubmissionObjectResult.validity_forces_review,
        )
        .join(SubmissionPage)
        .join(Submission)
        .where(Submission.submission_batch_id == batch.id)
    )
    return {row.id: tuple(row[1:]) for row in rows}


def _assert_forced_review(db, subs, owner) -> None:
    f05 = _in_review(db, subs["F05"], owner)
    for label in ("Q03", "Q04", "Q07"):
        assert INVALID_ANSWER in f05.get(label, ()), (label, f05)
    assert "MATRICOLA" in _in_review(db, subs["F09"], owner)


def test_threshold_and_idempotency(db, scan_storage, coll_multi):
    owner, batch, subs = upload_scenarios(
        db, coll_multi, "Threshold", [SCENARIOS[i] for i in THRESHOLD_IDS]
    )

    # Run, then run again at once.
    first = submission_batch_service.run_batch_extraction(db, batch.id, owner)
    assert first.errors == 0, first.error_details
    assert first.submissions_scanned == len(THRESHOLD_IDS)
    default = _state(db, batch, subs, owner)
    results = _result_count(db, batch)

    second = submission_batch_service.run_batch_extraction(db, batch.id, owner)
    assert second.errors == 0, second.error_details
    assert second.results_created == 0
    assert second.submissions_scanned == 0
    assert second.skipped_already_extracted == first.results_created
    assert _state(db, batch, subs, owner) == default
    assert _result_count(db, batch) == results

    # Every submission read without errors is SCANNED, and the default
    # counts are the scenarios' declared review.
    assert default == {
        i: (SubmissionStatus.SCANNED, len(SCENARIOS[i].expected.review)) for i in THRESHOLD_IDS
    }

    # 0.95, then 0.5. Blank questions and an ambiguous respondent ID stay in
    # review at every threshold; the readings themselves never change.
    readings = _readings(db, batch)
    submission_batch_service.update_confidence_threshold(db, batch.id, 0.95, owner)
    high = _state(db, batch, subs, owner)
    high_f07 = _in_review(db, subs["F07"], owner)
    _assert_forced_review(db, subs, owner)
    assert _readings(db, batch) == readings
    submission_batch_service.update_confidence_threshold(db, batch.id, 0.5, owner)
    low = _state(db, batch, subs, owner)
    low_f07 = _in_review(db, subs["F07"], owner)
    _assert_forced_review(db, subs, owner)
    assert _readings(db, batch) == readings

    # By direction only: no synthetic object has a confidence between the
    # default and 0.95 (full marks are 1.0, F07's weak ones already in
    # review), so 0.95 changes nothing. The count going up is shown by the
    # step from 0.5 back to the default below (F07, 4 → 6).
    for i in THRESHOLD_IDS:
        assert high[i][1] >= default[i][1] >= low[i][1], (i, high[i], default[i], low[i])
    assert low["F07"][1] < default["F07"][1]
    # F07's weak marks (0.66 and 0.61) leave review at 0.5; the cross and the
    # ring, read as blank, stay.
    assert {"Q01", "Q02"} <= set(high_f07)
    assert not {"Q01", "Q02"} & set(low_f07)
    assert {"Q03", "Q04"} <= set(low_f07)

    # Back to the system default.
    submission_batch_service.update_confidence_threshold(db, batch.id, None, owner)
    assert _state(db, batch, subs, owner) == default
    _assert_forced_review(db, subs, owner)
    assert _readings(db, batch) == readings

    counts = lambda state: {i: c for i, (_s, c) in state.items()}  # noqa: E731
    print(
        f"\n[synthetic] threshold: default {counts(default)}, 0.95 {counts(high)}, "
        f"0.5 {counts(low)}; first run {first.results_created} results, "
        f"second {second.results_created} (already read {second.skipped_already_extracted})"
    )


def test_review_anyway_after_missing_page_image(db, scan_storage, coll_multi):
    owner, batch, subs = upload_scenarios(db, coll_multi, "Review anyway", [REVIEW_ANYWAY])
    submission = subs["RA"]
    page_2 = next(p for p in submission.pages if p.form_variant_page.page_index == 2)
    Path(page_2.source_file_path).unlink()

    # The run fails on page 2 only; the submission stays MATCHED.
    summary = submission_batch_service.run_batch_extraction(db, batch.id, owner)
    assert summary.errors == len(PAGE_2_LABELS), summary.error_details
    assert {d["submission_page_id"] for d in summary.error_details} == {str(page_2.id)}
    assert summary.submissions_scanned == 0
    queued = next(
        s for s in submission_batch_service.get_batch_detail(db, batch.id, owner).submissions
        if s.id == submission.id
    )
    assert queued.status is SubmissionStatus.MATCHED
    assert queued.extraction_attempted
    assert queued.unread_object_count == len(PAGE_2_LABELS)

    result = review_anyway_service.review_anyway(db, submission.id, owner)
    assert result.status is SubmissionStatus.SCANNED
    assert result.placeholders_created == len(PAGE_2_LABELS)

    # Page 2's objects are in review as "Could not be read", next to the free
    # texts; everything else was read as on F01.
    in_review = _in_review(db, submission, owner)
    assert in_review == {
        **{label: frozenset({EXTRACTION_FAILED}) for label in PAGE_2_LABELS},
        "NOME": frozenset({ALWAYS_REVIEW}),
        "APERTA": frozenset({ALWAYS_REVIEW}),
    }
    queued = next(
        s for s in submission_batch_service.get_batch_detail(db, batch.id, owner).submissions
        if s.id == submission.id
    )
    assert queued.unread_object_count == 0
    assert queued.pending_review_count == len(in_review)
    print(
        f"\n[synthetic] review anyway: {summary.errors} errors on page 2, "
        f"{result.placeholders_created} placeholders, status {result.status.value}"
    )
