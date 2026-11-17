# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Loose images G01–G05 of the test kit.

The upload the operator makes with single image files instead of a PDF
(``upload_images``): grouping by filename, position from the VID or from the
name, two calls into one submission, a duplicate position, an ungroupable
sheet. G01 and G02 are full scenarios (``SCENARIOS``, real OCR, same ``check``
as the PDFs); G03–G05 stop at what the test kit says about them — grouping,
positions, audit — and need no OCR. Marked ``synthetic``: ``pytest -m synthetic
tests/synthetic -s``.
"""
from __future__ import annotations

import shutil
import time
import pytest
from sqlalchemy import select

from app.models.audit_log import AuditLog
from app.models.enums import SubmissionStatus
from app.models.submission import Submission
from app.models.user import User
from app.repositories import submission_batch_repo
from app.schemas.submission import SubmissionBatchCreate
from app.services import audit_service, submission_batch_service
from tests.synthetic.builders import build_coll_multi, build_coll_single
from tests.synthetic.runner import (
    check,
    loose_files,
    matching_of,
    read_outcome,
    report,
    scenario_sheet,
)
from tests.synthetic.scenarios import (
    DEDUCED,
    FILENAME,
    LOOSE_CASES,
    LOOSE_IDS,
    SCENARIOS,
)

pytestmark = pytest.mark.synthetic

_needs_ocr = pytest.mark.skipif(
    shutil.which("tesseract") is None,
    reason="Tesseract not installed: the scenarios use real OCR",
)
_BUILDERS = {"MULTI": build_coll_multi, "SINGLE": build_coll_single}


def _batch(db, project, name):
    owner = db.get(User, project.owner_id)
    batch = submission_batch_service.create_batch(
        db, project.project_id, SubmissionBatchCreate(name=name), owner
    )
    return owner, batch


def _assert_declared(case, status, missing_pages) -> None:
    """What the truth sheet of the kit says about the case (``LOOSE_CASES``)
    is what the app does: the kit is the verified reference."""
    assert status is case.status_after_upload
    if case.missing_pages is not None:  # None: the kit does not declare it
        assert list(missing_pages) == list(case.missing_pages)


def _files(db, project, case_id):
    """The case's ``(name, bytes)`` files, declared in ``LOOSE_CASES``."""
    return [named for _f, named in loose_files(db, project, LOOSE_CASES[case_id])]


@_needs_ocr
@pytest.mark.parametrize("scenario_id", LOOSE_IDS)
def test_loose_scenario(db, scan_storage, scenario_id):
    """G01, G02: one submission, then the same checks as a PDF scenario."""
    scenario = SCENARIOS[scenario_id]
    project = _BUILDERS[scenario.project](db)
    timings: dict[str, float] = {}

    start = time.perf_counter()
    files = scenario_sheet(db, project, scenario).to_images(scenario.image_names)
    timings["generate"] = time.perf_counter() - start

    start = time.perf_counter()
    owner, batch = _batch(db, project, scenario.id)
    ingest = submission_batch_service.upload_images(db, batch.id, files, owner)
    timings["upload"] = time.perf_counter() - start
    assert ingest.rejected == []
    assert ingest.unassigned_count == 0
    assert ingest.pages_added == len(files)
    assert len(ingest.submissions_created) == 1, "the images go into a single submission"
    submission = db.get(Submission, ingest.submissions_created[0])
    matching = matching_of(submission)

    start = time.perf_counter()
    summary = submission_batch_service.run_batch_extraction(db, batch.id, owner)
    timings["extract"] = time.perf_counter() - start

    outcome = read_outcome(db, scenario.id, submission, matching, summary, owner, timings)
    notes: list[str] = []
    errors = check(outcome, project, scenario, notes)
    report(outcome, notes)
    assert not errors, f"{scenario_id}: {len(errors)} discrepancies\n" + "\n".join(
        f"  - {e}" for e in errors
    )


def test_g03_two_calls_same_group(db, scan_storage, coll_single):
    """Page 1 in one upload, page 2 in a second one: the same submission."""
    page_1, page_2 = _files(db, coll_single, "G03")
    owner, batch = _batch(db, coll_single, "G03")

    first = submission_batch_service.upload_images(db, batch.id, [page_1], owner)
    second = submission_batch_service.upload_images(db, batch.id, [page_2], owner)

    assert len(first.submissions_created) == 1
    assert second.submissions_created == []
    assert second.submissions_updated == first.submissions_created
    submission = db.get(Submission, first.submissions_created[0])
    db.refresh(submission)
    matching = matching_of(submission)
    assert matching.status is SubmissionStatus.MATCHED
    assert matching.pages == ((1, FILENAME, DEDUCED), (2, FILENAME, DEDUCED))
    assert submission.missing_page_indices == []
    _assert_declared(LOOSE_CASES["G03"], matching.status, submission.missing_page_indices)
    detail = submission_batch_service.get_batch_detail(db, batch.id, owner)
    assert [s.id for s in detail.submissions] == [submission.id]


def test_g04_duplicate_position(db, scan_storage, coll_single):
    """Two different sheets both named ``bianchi_pagina_1.jpg``: both kept,
    the duplicate position recorded in the audit trail."""
    files = _files(db, coll_single, "G04")
    assert files[0][1] != files[1][1]
    owner, batch = _batch(db, coll_single, "G04")

    ingest = submission_batch_service.upload_images(db, batch.id, files, owner)

    assert ingest.rejected == []
    assert ingest.pages_added == 2
    assert len(ingest.submissions_created) == 1
    submission = db.get(Submission, ingest.submissions_created[0])
    matching = matching_of(submission)
    assert matching.status is SubmissionStatus.MATCHED
    # Both declare page 1 by name; page 2 is missing.
    assert matching.pages == ((1, FILENAME, DEDUCED), (1, FILENAME, DEDUCED))
    assert submission.missing_page_indices == [2]
    _assert_declared(LOOSE_CASES["G04"], matching.status, submission.missing_page_indices)
    audit = db.scalars(
        select(AuditLog).where(
            AuditLog.action == audit_service.SUBMISSION_IMAGES_INGESTED,
            AuditLog.entity_id == str(batch.id),
        )
    ).one()
    assert audit.metadata_json["pages_with_duplicate_position"] == [
        {"filename": "bianchi_pagina_1.jpg", "source_page_order": 0}
    ]


def test_g05_random_name_unassigned(db, scan_storage, coll_single):
    """A COLL-SINGLE sheet with a random name and no QR: an UNASSIGNED
    single-page submission, left out of the extraction run."""
    owner, batch = _batch(db, coll_single, "G05")

    ingest = submission_batch_service.upload_images(
        db, batch.id, _files(db, coll_single, "G05"), owner
    )

    assert ingest.unassigned_count == 1
    assert len(ingest.submissions_created) == 1
    submission = db.get(Submission, ingest.submissions_created[0])
    assert submission.status is SubmissionStatus.UNASSIGNED
    _assert_declared(LOOSE_CASES["G05"], submission.status, submission.missing_page_indices)

    summary = submission_batch_service.run_batch_extraction(db, batch.id, owner)
    assert summary.submissions_processed == 0
    assert summary.results_created == 0
    db.refresh(submission)
    assert submission.status is SubmissionStatus.UNASSIGNED

    # Blocks the export until marked unresolvable; undoing brings it back to
    # UNASSIGNED. Alone in its batch, marked it leaves a batch of unresolvable
    # submissions only: nothing blocks it, nothing to export.
    def readiness():
        return submission_batch_service.export_readiness(
            submission_batch_repo.status_counts_for_batch(db, batch.id)
        )

    assert readiness().unassigned == 1
    marked = submission_batch_service.mark_unresolvable(db, submission.id, owner)
    assert marked.status is SubmissionStatus.UNRESOLVABLE
    assert readiness().unassigned == 0
    undone = submission_batch_service.undo_unresolvable(db, submission.id, owner)
    assert undone.status is SubmissionStatus.UNASSIGNED
    assert readiness().unassigned == 1
