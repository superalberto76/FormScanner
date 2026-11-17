# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for serving a scanned sheet's image.

``GET /submission-pages/{page_id}/image`` returns the WHOLE stored scan of one
SubmissionPage, which the review focus panel crops client-side. What this suite
pins down:

  * the bytes come back verbatim, typed from their CONTENT: a rasterised PDF
    page is ``image/png``, a loose JPEG upload ``image/jpeg``, and bytes that
    are no image ``application/octet-stream`` — whatever the extension says;
  * the private, always-revalidated cache header with its ETag (a long
    ``max-age`` would assume a file that is never rewritten);
  * authorisation through the submission (403 non-member, 404 unknown sheet);
  * a missing file, or a path outside the storage directory, is 404 "Scan
    image not found.";
  * any status and CLOSED projects are served;
  * the read writes nothing.

Skipped without TEST_DATABASE_URL, like the rest of tests/integration.
"""
from __future__ import annotations

import uuid
from io import BytesIO

import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import event

from app.api.v1.endpoints.submission_pages import get_submission_page_image
from app.core.config import settings
from app.models.enums import (
    FormProjectStatus,
    FormProjectType,
    PageOrderSource,
    ProjectRole,
    SubmissionStatus,
    UserRole,
)
from app.models.form_project import FormProject
from app.models.form_project_member import FormProjectMember
from app.models.submission import Submission
from app.models.submission_batch import SubmissionBatch
from app.models.submission_page import SubmissionPage
from app.models.user import User
from app.services import storage_service


def _image_bytes(fmt: str) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (40, 30), "white").save(buf, format=fmt)
    return buf.getvalue()


PNG_BYTES = _image_bytes("PNG")
JPEG_BYTES = _image_bytes("JPEG")


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(
        settings, "submission_pages_dir", str(tmp_path / "submission_pages")
    )


def _make_user(db) -> User:
    u = User(
        email=f"page-image-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.USER,
    )
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def actor(db) -> User:
    return _make_user(db)


@pytest.fixture
def project(db, actor) -> FormProject:
    project = FormProject(
        owner_id=actor.id,
        type=FormProjectType.EXAM,
        name="Page image project",
        status=FormProjectStatus.ACTIVE,
    )
    db.add(project)
    db.flush()
    db.add(
        FormProjectMember(
            project_id=project.id, user_id=actor.id, role=ProjectRole.OWNER
        )
    )
    db.flush()
    return project


@pytest.fixture
def batch(db, project, actor) -> SubmissionBatch:
    batch = SubmissionBatch(
        form_project_id=project.id,
        name="Scan batch",
        created_by_user_id=actor.id,
    )
    db.add(batch)
    db.flush()
    return batch


def _make_sheet(
    db,
    batch,
    *,
    content: bytes | None = PNG_BYTES,
    ext: str = ".png",
    status: SubmissionStatus = SubmissionStatus.SCANNED,
) -> SubmissionPage:
    """One submission with one sheet whose image is stored the way ingestion
    stores it: a rasterised PDF page via ``save_submission_page_image``, a
    loose upload via ``save_submission_page_upload``. *content* None leaves
    the row pointing at a file that was never written."""
    submission = Submission(submission_batch_id=batch.id, status=status)
    db.add(submission)
    db.flush()
    page_id = uuid.uuid4()
    if content is None:
        path = storage_service.submission_page_image_path(page_id, ext)
    elif ext == ".png":
        path = storage_service.save_submission_page_image(content, page_id)
    else:
        path = storage_service.save_submission_page_upload(content, page_id, ext)
    sheet = SubmissionPage(
        id=page_id,
        submission_id=submission.id,
        source_file_path=str(path),
        source_filename=f"sheet{ext}",
        source_page_order=0,
        page_order_source=PageOrderSource.PDF_ORDER,
    )
    db.add(sheet)
    db.flush()
    return sheet


def _get(db, sheet_or_id, user):
    page_id = getattr(sheet_or_id, "id", sheet_or_id)
    return get_submission_page_image(page_id=page_id, db=db, user=user)


def _assert_404(db, page_id, user, detail):
    with pytest.raises(HTTPException) as excinfo:
        _get(db, page_id, user)
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == detail


# ---------------------------------------------------------------------------
# Content and headers
# ---------------------------------------------------------------------------


def test_a_png_sheet_is_served_verbatim_as_png(db, actor, batch):
    sheet = _make_sheet(db, batch)

    response = _get(db, sheet, actor)

    assert response.status_code == 200
    assert response.body == PNG_BYTES
    assert response.media_type == "image/png"
    assert response.headers["content-type"] == "image/png"


def test_a_loose_jpeg_upload_is_served_as_jpeg(db, actor, batch):
    sheet = _make_sheet(db, batch, content=JPEG_BYTES, ext=".jpg")

    response = _get(db, sheet, actor)

    assert response.body == JPEG_BYTES
    assert response.media_type == "image/jpeg"


def test_the_type_comes_from_the_content_not_the_extension(db, actor, batch):
    sheet = _make_sheet(db, batch, content=JPEG_BYTES, ext=".png")

    assert _get(db, sheet, actor).media_type == "image/jpeg"


def test_bytes_that_are_no_image_are_octet_stream(db, actor, batch):
    sheet = _make_sheet(db, batch, content=b"not an image", ext=".png")

    response = _get(db, sheet, actor)

    assert response.body == b"not an image"
    assert response.media_type == "application/octet-stream"


def test_the_private_cache_header_is_set(db, actor, batch):
    response = _get(db, _make_sheet(db, batch), actor)

    assert response.headers["cache-control"] == "private, no-cache"
    assert response.headers["etag"].startswith('W/"')
    assert "content-disposition" not in response.headers


# ---------------------------------------------------------------------------
# Authorisation and missing files
# ---------------------------------------------------------------------------


def test_a_non_member_gets_the_usual_403(db, batch):
    sheet = _make_sheet(db, batch)
    outsider = _make_user(db)

    with pytest.raises(HTTPException) as excinfo:
        _get(db, sheet, outsider)

    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "You are not a member of this project"


def test_an_unknown_sheet_is_404(db, actor):
    _assert_404(db, uuid.uuid4(), actor, "Submission page not found")


def test_a_file_deleted_from_disk_is_404(db, actor, batch):
    sheet = _make_sheet(db, batch)
    storage_service.assert_within_submission_pages_dir(
        sheet.source_file_path
    ).unlink()

    _assert_404(db, sheet.id, actor, "Scan image not found.")


def test_a_file_never_written_is_404(db, actor, batch):
    sheet = _make_sheet(db, batch, content=None)

    _assert_404(db, sheet.id, actor, "Scan image not found.")


def test_a_path_outside_the_storage_directory_is_404(db, actor, batch, tmp_path):
    outside = tmp_path / "elsewhere.png"
    outside.write_bytes(PNG_BYTES)
    sheet = _make_sheet(db, batch)
    sheet.source_file_path = str(outside)
    db.flush()

    _assert_404(db, sheet.id, actor, "Scan image not found.")


# ---------------------------------------------------------------------------
# Status, project state, no writes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "submission_status", [SubmissionStatus.REVIEWED, SubmissionStatus.FINALIZED]
)
def test_any_status_is_served(db, actor, batch, submission_status):
    sheet = _make_sheet(db, batch, status=submission_status)

    assert _get(db, sheet, actor).body == PNG_BYTES


@pytest.mark.parametrize(
    "project_status", [FormProjectStatus.CLOSED, FormProjectStatus.ARCHIVED]
)
def test_a_closed_project_is_served(db, actor, project, batch, project_status):
    sheet = _make_sheet(db, batch, status=SubmissionStatus.REVIEWED)
    project.status = project_status
    db.flush()

    assert _get(db, sheet, actor).body == PNG_BYTES


def test_serving_writes_nothing(db, actor, batch):
    sheet = _make_sheet(db, batch)
    db.refresh(sheet.submission)
    before = (sheet.submission.updated_at, sheet.updated_at)

    statements: list[str] = []

    def _record(conn, cursor, statement, *args):
        statements.append(statement)

    engine = db.get_bind().engine
    event.listen(engine, "before_cursor_execute", _record)
    try:
        _get(db, sheet, actor)
        db.flush()
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert not [s for s in statements if not s.lstrip().upper().startswith("SELECT")]
    db.expire_all()
    after = db.get(SubmissionPage, sheet.id)
    assert (after.submission.updated_at, after.updated_at) == before
