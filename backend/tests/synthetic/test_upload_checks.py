# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wrong uploads and project configuration, U01–U05 of the test kit.

At service level (no ``TestClient``), with minimal files instead of filled-in
sheets — so, unlike the rest of this folder, NOT marked ``synthetic``: they run
in the default suite. The FE side (the filter that drops a ``.docx``, the
scan-readiness banner) is covered by the FE tests.

U05: the server applies scan-readiness too, refusing batch creation and
uploads with the structured 409 ``NOT_SCAN_READY``.
"""
from __future__ import annotations

import io
import zipfile

import fitz
import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import select

from app.core.config import settings
from app.models.enums import FormProjectStatus, SubmissionStatus
from app.models.submission import Submission
from app.models.submission_batch import SubmissionBatch
from app.models.user import User
from app.repositories import submission_batch_repo
from app.schemas.submission import SubmissionBatchCreate
from app.services import (
    form_project_service,
    submission_batch_service,
    submission_lock_service,
    submission_review_service,
)


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("L", (200, 280), 255).save(out, "PNG")
    return out.getvalue()


def _pdf(pages: int) -> bytes:
    doc = fitz.open()
    try:
        for _ in range(pages):
            doc.new_page(width=595, height=842)
        return doc.tobytes()
    finally:
        doc.close()


def _docx() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<document/>")
    return out.getvalue()


def _batch(db, project, name="Kit"):
    owner = db.get(User, project.owner_id)
    batch = submission_batch_service.create_batch(
        db, project.project_id, SubmissionBatchCreate(name=name), owner
    )
    return owner, batch


def _submissions(db, batch) -> list[Submission]:
    return list(db.scalars(select(Submission).where(Submission.submission_batch_id == batch.id)))


def _refused(call, status_code: int) -> HTTPException:
    with pytest.raises(HTTPException) as refused:
        call()
    assert refused.value.status_code == status_code, refused.value.detail
    return refused.value


# ---------------------------------------------------------------------------
# U01–U03
# ---------------------------------------------------------------------------


def test_u01_unsupported_file_rejected_others_uploaded(db, scan_storage, coll_single):
    owner, batch = _batch(db, coll_single)
    files = [
        ("u01_pagina_1.png", _png()),
        ("report.docx", _docx()),
        ("u01_pagina_2.png", _png()),
    ]

    ingest = submission_batch_service.upload_images(db, batch.id, files, owner)

    assert [(r.filename, r.reason) for r in ingest.rejected] == [
        ("report.docx", "Unsupported or invalid image. Allowed: PNG, JPEG, BMP, SVG.")
    ]
    assert ingest.pages_added == 2
    assert len(ingest.submissions_created) == 1


def test_u02_empty_and_corrupt_pdf_rejected_others_proceed(db, scan_storage, coll_single):
    owner, batch = _batch(db, coll_single)

    empty = _refused(
        lambda: submission_batch_service.upload_pdf(db, batch.id, "empty.pdf", b"", owner), 422
    )
    assert empty.detail == "Uploaded file is empty."
    corrupt = _refused(
        lambda: submission_batch_service.upload_pdf(
            db, batch.id, "note.pdf", b"notes, not a PDF\n" * 20, owner
        ),
        422,
    )
    assert corrupt.detail == "File could not be read as a PDF."
    # What the end of the refused request does to its session.
    db.rollback()

    ok = submission_batch_service.upload_pdf(db, batch.id, "good.pdf", _pdf(2), owner)
    assert ok.status is SubmissionStatus.MATCHED
    assert [s.id for s in _submissions(db, batch)] == [ok.id]


def test_u03_pdf_over_limit(db, scan_storage, coll_single, monkeypatch):
    monkeypatch.setattr(settings, "max_submission_pdf_bytes", 1024 * 1024)
    owner, batch = _batch(db, coll_single)

    too_large = _refused(
        lambda: submission_batch_service.upload_pdf(
            db, batch.id, "large.pdf", b"%PDF-1.4\n" + b"0" * (1024 * 1024), owner
        ),
        413,
    )
    assert too_large.detail == "PDF is too large (max 1 MB)."
    assert _submissions(db, batch) == []


# ---------------------------------------------------------------------------
# U04 — CLOSED project
# ---------------------------------------------------------------------------


def test_u04_closed_project_no_upload_review_allowed(db, scan_storage, coll_single):
    owner, batch = _batch(db, coll_single)
    submission = submission_batch_service.upload_pdf(db, batch.id, "u04.pdf", _pdf(2), owner)
    form_project_service.transition_project(
        db, coll_single.project_id, FormProjectStatus.CLOSED, owner
    )

    # Collecting is over: no new batch, no upload into an existing one.
    _refused(lambda: _batch(db, coll_single, "After closing"), 409)
    _refused(
        lambda: submission_batch_service.upload_pdf(db, batch.id, "late.pdf", _pdf(2), owner),
        409,
    )
    _refused(
        lambda: submission_batch_service.upload_images(
            db, batch.id, [("late_pagina_1.png", _png())], owner
        ),
        409,
    )

    # Reading what was collected goes on: the run, the review read, the lock.
    summary = submission_batch_service.run_batch_extraction(db, batch.id, owner)
    assert summary.errors == 0, summary.error_details
    assert summary.submissions_scanned == 1
    detail = submission_review_service.get_review_detail(db, submission.id, owner)
    assert detail.status is SubmissionStatus.SCANNED
    assert submission_lock_service.acquire(db, submission.id, owner).acquired


# ---------------------------------------------------------------------------
# U05 — scan-readiness (COLL-CONFIG: two published variants, no VID)
# ---------------------------------------------------------------------------


def test_u05_scan_readiness_not_ready(db, coll_config):
    owner = db.get(User, coll_config.owner_id)
    readiness = form_project_service.get_scan_readiness(db, coll_config.project_id, owner)
    assert readiness.ready is False
    assert readiness.published_variant_count == 2
    assert "VARIANT_IDENTIFIER" in readiness.reason


def _batch_row(db, project) -> SubmissionBatch:
    """A batch of COLL-CONFIG written straight to the table: the one a project
    could hold from before its configuration stopped being ready (``create_batch``
    refuses it now)."""
    batch = submission_batch_repo.add(
        db,
        SubmissionBatch(
            form_project_id=project.project_id, name="U05", created_by_user_id=project.owner_id
        ),
    )
    db.commit()
    return batch


def _not_scan_ready(call) -> None:
    refused = _refused(call, 409)
    assert refused.detail["code"] == "NOT_SCAN_READY"
    assert "VARIANT_IDENTIFIER" in refused.detail["message"]


def test_u05_create_batch_refused(db, coll_config):
    _not_scan_ready(lambda: _batch(db, coll_config, "U05"))


def test_u05_upload_pdf_refused(db, scan_storage, coll_config):
    owner = db.get(User, coll_config.owner_id)
    batch = _batch_row(db, coll_config)
    _not_scan_ready(
        lambda: submission_batch_service.upload_pdf(db, batch.id, "u05.pdf", _pdf(1), owner)
    )


def test_u05_upload_images_refused(db, scan_storage, coll_config):
    owner = db.get(User, coll_config.owner_id)
    batch = _batch_row(db, coll_config)
    _not_scan_ready(
        lambda: submission_batch_service.upload_images(
            db, batch.id, [("u05_pagina_1.png", _png())], owner
        )
    )
