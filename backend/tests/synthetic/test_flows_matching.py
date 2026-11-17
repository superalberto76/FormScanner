# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Manual variant assignment after MATCH_FAILED.

F18 (every VID covered) and F20 (variant D, left DRAFT) land in MATCH_FAILED,
as the single-pass scenarios check. Here the operator's next step: F18 gets
variant A by hand → MATCHED, sheets bound by physical order, and a new run
brings it to SCANNED; the DRAFT variant D is refused for both.

Each one records why it failed (F18 no readable code, F20 the value of D,
read but not published); F20 marked unresolvable no longer blocks the batch,
matching leaves it alone even once D is published, and undoing the verdict
lets a re-run match it. With F20 unresolvable beside it, closing F01 makes
the batch ready for export. Marked ``synthetic``:
``pytest -m synthetic tests/synthetic -s``.
"""
from __future__ import annotations

import shutil

import pytest
from fastapi import HTTPException

from app.models.enums import MatchFailureReason, SubmissionStatus
from app.models.user import User
from app.schemas.submission import SubmissionBatchCreate
from app.services import (
    form_variant_service,
    submission_batch_service,
    submission_close_service,
    submission_review_service,
)
from tests.synthetic.runner import review_objects, scenario_pdf
from tests.synthetic.scenarios import (
    ALWAYS_REVIEW,
    DEDUCED,
    PDF_ORDER,
    SCENARIOS,
)

pytestmark = [
    pytest.mark.synthetic,
    pytest.mark.skipif(
        shutil.which("tesseract") is None,
        reason="Tesseract not installed: the scenarios use real OCR",
    ),
]


def _upload(db, project, scenario_id):
    owner = db.get(User, project.owner_id)
    batch = submission_batch_service.create_batch(
        db, project.project_id, SubmissionBatchCreate(name=scenario_id), owner
    )
    submission = submission_batch_service.upload_pdf(
        db, batch.id, f"{scenario_id}.pdf",
        scenario_pdf(db, project, SCENARIOS[scenario_id]), owner,
    )
    assert submission.status is SubmissionStatus.MATCH_FAILED
    return owner, batch, submission


def _assert_draft_refused(db, project, submission, owner):
    with pytest.raises(HTTPException) as refused:
        submission_batch_service.assign_variant_manually(
            db, submission.id, project.variants["D"].variant_id, owner
        )
    assert refused.value.status_code == 409
    db.refresh(submission)
    assert submission.status is SubmissionStatus.MATCH_FAILED
    assert submission.matched_form_variant_id is None


def test_f18_manual_assignment_then_new_run(db, scan_storage, coll_multi):
    owner, batch, submission = _upload(db, coll_multi, "F18")
    assert submission.match_failure_reason is MatchFailureReason.NO_VID_FOUND
    assert submission.match_failure_code_value is None

    # A run before the assignment leaves it alone.
    summary = submission_batch_service.run_batch_extraction(db, batch.id, owner)
    assert summary.submissions_processed == 0
    db.refresh(submission)
    assert submission.status is SubmissionStatus.MATCH_FAILED

    _assert_draft_refused(db, coll_multi, submission, owner)

    submission = submission_batch_service.assign_variant_manually(
        db, submission.id, coll_multi.variants["A"].variant_id, owner
    )
    assert submission.status is SubmissionStatus.MATCHED
    assert submission.matched_form_variant_id == coll_multi.variants["A"].variant_id
    assert submission.match_failure_reason is None
    # No VID readable: the sheets take their template page from physical order.
    pages = [
        (p.form_variant_page.page_index, p.page_order_source, p.match_confidence)
        for p in sorted(submission.pages, key=lambda p: p.source_page_order)
    ]
    assert pages == [(1, PDF_ORDER, DEDUCED), (2, PDF_ORDER, DEDUCED), (3, PDF_ORDER, DEDUCED)]

    summary = submission_batch_service.run_batch_extraction(db, batch.id, owner)
    assert summary.errors == 0, summary.error_details
    assert summary.submissions_scanned == 1
    detail = submission_review_service.get_review_detail(db, submission.id, owner)
    assert detail.status is SubmissionStatus.SCANNED
    assert detail.respondent_identifier == "100018"
    assert detail.missing_page_indices == []
    # A clean sheet: in review only the free texts, as on F01.
    reasons = {k: set(o.flag_reasons) for k, o in review_objects(detail).items()}
    assert reasons == {"NOME": {ALWAYS_REVIEW}, "APERTA": {ALWAYS_REVIEW}}
    print(
        f"\n[synthetic] F18: assigned A, pages "
        + ", ".join(f"{n}:{source.value}:{conf}" for n, source, conf in pages)
        + f"; run: {summary.results_created} results, status {detail.status.value}"
    )


def test_f20_draft_variant_not_assignable(db, scan_storage, coll_multi):
    owner, _batch, submission = _upload(db, coll_multi, "F20")
    assert submission.match_failure_reason is MatchFailureReason.VID_NOT_RECOGNISED
    assert submission.match_failure_code_value == coll_multi.variants["D"].vid_value
    _assert_draft_refused(db, coll_multi, submission, owner)


def _batch_item(db, project, batch, owner):
    items = submission_batch_service.list_batches_for_project(
        db, project.project_id, owner
    )
    return next(item for item in items if item.id == batch.id)


def test_f20_unresolvable_then_undone(db, scan_storage, coll_multi):
    """Marked unresolvable, F20 is closed — not counted as to assign, and
    alone in its batch it makes no export — and a re-run after D is published
    leaves it alone; once undone, the re-run matches it to D."""
    owner, batch, submission = _upload(db, coll_multi, "F20")

    submission = submission_batch_service.mark_unresolvable(db, submission.id, owner)
    assert submission.status is SubmissionStatus.UNRESOLVABLE
    item = _batch_item(db, coll_multi, batch, owner)
    assert (item.match_failed_count, item.unresolvable_count) == (0, 1)
    assert item.ready_for_export is False

    variant_d = coll_multi.variants["D"].variant_id
    form_variant_service.publish_variant(db, variant_d, owner)
    summary = submission_batch_service.run_batch_matching(db, batch.id, owner)
    assert summary.submissions_processed == 0
    db.refresh(submission)
    assert submission.status is SubmissionStatus.UNRESOLVABLE
    assert submission.matched_form_variant_id is None

    submission = submission_batch_service.undo_unresolvable(db, submission.id, owner)
    assert submission.status is SubmissionStatus.MATCH_FAILED
    assert submission.match_failure_reason is MatchFailureReason.VID_NOT_RECOGNISED
    summary = submission_batch_service.run_batch_matching(db, batch.id, owner)
    assert summary.matched == 1
    db.refresh(submission)
    assert submission.status is SubmissionStatus.MATCHED
    assert submission.matched_form_variant_id == variant_d
    assert submission.match_failure_reason is None
    print(
        "\n[synthetic] F20: unresolvable, re-run after publishing D "
        "has no effect; undone → MATCHED on D"
    )


def test_f20_unresolvable_beside_a_closed_submission_makes_the_batch_ready(
    db, scan_storage, coll_multi
):
    """F20 marked unresolvable does not hold the batch back, and F01,
    extracted and closed with its free texts left unresolved, is what makes
    the export possible."""
    owner, batch, f20 = _upload(db, coll_multi, "F20")
    f01 = submission_batch_service.upload_pdf(
        db, batch.id, "F01.pdf", scenario_pdf(db, coll_multi, SCENARIOS["F01"]), owner
    )
    submission_batch_service.mark_unresolvable(db, f20.id, owner)
    submission_batch_service.run_batch_extraction(db, batch.id, owner)
    db.refresh(f01)
    assert f01.status is SubmissionStatus.SCANNED
    assert _batch_item(db, coll_multi, batch, owner).ready_for_export is False

    unresolved = len(SCENARIOS["F01"].expected.review)
    detail = submission_close_service.close_submission(db, f01.id, owner, f01.review_version)

    assert detail.status is SubmissionStatus.REVIEWED
    assert detail.pending_review_count == unresolved
    # Only its free texts left, which do not score.
    db.refresh(f01)
    assert f01.total_score == SCENARIOS["F01"].expected.score.points == 28
    item = _batch_item(db, coll_multi, batch, owner)
    assert (item.reviewed_count, item.unresolvable_count, item.pending_review_count) == (1, 1, 0)
    assert item.ready_for_export is True
    print(
        f"\n[synthetic] F20 unresolvable + F01 closed with {unresolved} unresolved: "
        "batch ready for export"
    )
