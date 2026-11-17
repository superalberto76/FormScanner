# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for the scan-readiness gate on ingestion.

The scan-readiness check is blocking "before the batch is uploaded/started",
with no bypass. The FE shows it (banner, "+ New batch" disabled), and the server
enforces the same rule (``compute_scan_readiness``) too:

  * ``create_batch``, ``upload_pdf`` and ``upload_images`` refuse a project that
    is not ready with the structured 409 ``NOT_SCAN_READY``, whose ``message`` is
    the readiness ``reason`` (the banner's text), plus ``published_variant_count``
    and, for a VARIANT_IDENTIFIER collision, ``colliding_variant_ids``;
  * the uploads re-check on every call, so a batch opened while the project was
    ready refuses new files once it is not (a second variant published without
    VARIANT_IDENTIFIER), and nothing is written;
  * the ACTIVE gate speaks first (a CLOSED project keeps its own message), and a
    non-member never learns about readiness.

Only ingestion is gated: matching, extraction and review of the submissions
already uploaded stay as they were. The
readiness rule itself is covered by test_scan_readiness_integration.py and
test_variant_identifier_uniqueness_integration.py.

Variants are built directly (variant → page → shared CODE object → binding), as
in the uniqueness suite: the gate reads the stored end state, and a collision can
only exist as data written outside ``publish_variant``'s own gate.
Skipped without TEST_DATABASE_URL.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import fitz  # PyMuPDF
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.core.config import settings
from app.models.code_object_config import CodeObjectConfig
from app.models.enums import (
    CodeType,
    FormProjectStatus,
    FormProjectType,
    FormVariantStatus,
    PageObjectRole,
    PageObjectType,
    ProjectRole,
    UserRole,
)
from app.models.form_project import FormProject
from app.models.form_project_member import FormProjectMember
from app.models.form_variant import FormVariant
from app.models.form_variant_page import FormVariantPage
from app.models.form_variant_role_binding import FormVariantRoleBinding
from app.models.page_object import PageObject
from app.models.submission import Submission
from app.models.submission_batch import SubmissionBatch
from app.models.user import User
from app.schemas.submission import SubmissionBatchCreate
from app.services import form_project_service, submission_batch_service


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _submission_storage(tmp_path, monkeypatch):
    """Redirect both submission storage dirs into a throwaway tmp_path."""
    monkeypatch.setattr(
        settings, "submission_originals_dir", str(tmp_path / "submission_originals")
    )
    monkeypatch.setattr(
        settings, "submission_pages_dir", str(tmp_path / "submission_pages")
    )


@pytest.fixture
def owner(db) -> User:
    return _make_user(db)


def _make_user(db) -> User:
    u = User(
        email=f"scan-gate-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.USER,
    )
    db.add(u)
    db.flush()
    return u


def _make_project(db, owner, *, status=FormProjectStatus.ACTIVE) -> FormProject:
    project = FormProject(
        owner_id=owner.id,
        type=FormProjectType.EXAM,
        name="Scan gate project",
        status=status,
    )
    db.add(project)
    db.flush()
    db.add(
        FormProjectMember(
            project_id=project.id, user_id=owner.id, role=ProjectRole.OWNER
        )
    )
    db.flush()
    return project


def _make_variant(
    db, project, owner, *, version_number: int, identifier_value: str | None = None
) -> FormVariant:
    """A one-page PUBLISHED variant, optionally carrying a VARIANT_IDENTIFIER."""
    variant = FormVariant(
        form_project_id=project.id,
        root_key="root",
        name=f"v{version_number}",
        status=FormVariantStatus.PUBLISHED,
        version_number=version_number,
        created_by_user_id=owner.id,
    )
    db.add(variant)
    db.flush()
    page = FormVariantPage(form_variant_id=variant.id, page_index=1)
    db.add(page)
    db.flush()

    if identifier_value is not None:
        obj = PageObject(
            form_variant_page_id=page.id,
            object_type=PageObjectType.CODE,
            x=0, y=0, width=50, height=50, label="ident",
            shared_group_id=uuid.uuid4(),
            shared_across_pages=True,
        )
        db.add(obj)
        db.flush()
        db.add(
            CodeObjectConfig(
                page_object_id=obj.id, code_type=CodeType.QR, value=identifier_value
            )
        )
        db.add(
            FormVariantRoleBinding(
                form_variant_id=variant.id,
                page_object_id=obj.id,
                role=PageObjectRole.VARIANT_IDENTIFIER,
            )
        )
        db.flush()
    return variant


def _create_batch(db, project, user) -> SubmissionBatch:
    return submission_batch_service.create_batch(
        db, project.id, SubmissionBatchCreate(name="Batch"), user
    )


def _pdf_bytes() -> bytes:
    doc = fitz.open()
    try:
        doc.new_page().insert_text((72, 72), "Page 0")
        return doc.tobytes()
    finally:
        doc.close()


def _png_bytes() -> bytes:
    doc = fitz.open()
    try:
        page = doc.new_page(width=100, height=100)
        return page.get_pixmap().tobytes("png")
    finally:
        doc.close()


def _not_scan_ready(call) -> dict:
    with pytest.raises(HTTPException) as refused:
        call()
    assert refused.value.status_code == 409
    detail = refused.value.detail
    assert detail["code"] == "NOT_SCAN_READY"
    return detail


def _batch_count(db, project) -> int:
    return db.scalar(
        select(func.count())
        .select_from(SubmissionBatch)
        .where(SubmissionBatch.form_project_id == project.id)
    )


def _submission_count(db, batch) -> int:
    return db.scalar(
        select(func.count())
        .select_from(Submission)
        .where(Submission.submission_batch_id == batch.id)
    )


def _stored_files() -> list[Path]:
    roots = [Path(settings.submission_originals_dir), Path(settings.submission_pages_dir)]
    return [f for root in roots if root.exists() for f in root.rglob("*") if f.is_file()]


# ---------------------------------------------------------------------------
# create_batch
# ---------------------------------------------------------------------------


def test_no_published_variant_refuses_batch_creation(db, owner):
    project = _make_project(db, owner)

    detail = _not_scan_ready(lambda: _create_batch(db, project, owner))

    assert detail["published_variant_count"] == 0
    assert "colliding_variant_ids" not in detail
    assert _batch_count(db, project) == 0


def test_several_variants_without_identifier_refuse_batch_creation(db, owner):
    project = _make_project(db, owner)
    _make_variant(db, project, owner, version_number=1)
    _make_variant(db, project, owner, version_number=2)

    detail = _not_scan_ready(lambda: _create_batch(db, project, owner))

    assert detail["published_variant_count"] == 2
    assert "VARIANT_IDENTIFIER" in detail["message"]
    assert _batch_count(db, project) == 0


def test_a_shared_identifier_value_refuses_batch_creation(db, owner):
    project = _make_project(db, owner)
    a = _make_variant(db, project, owner, version_number=1, identifier_value="SAME")
    b = _make_variant(db, project, owner, version_number=2, identifier_value="SAME")

    detail = _not_scan_ready(lambda: _create_batch(db, project, owner))

    assert detail["published_variant_count"] == 2
    assert detail["colliding_variant_ids"] == sorted([str(a.id), str(b.id)])
    assert _batch_count(db, project) == 0


def test_the_refusal_carries_the_banner_reason(db, owner):
    """The 409's message is the readiness reason verbatim: the banner and the
    refusal can never tell the operator two different things."""
    project = _make_project(db, owner)
    _make_variant(db, project, owner, version_number=1)
    _make_variant(db, project, owner, version_number=2)
    reason = form_project_service.get_scan_readiness(db, project.id, owner).reason

    detail = _not_scan_ready(lambda: _create_batch(db, project, owner))

    assert detail["message"] == reason


def test_a_single_published_variant_opens_a_batch(db, owner):
    project = _make_project(db, owner)
    _make_variant(db, project, owner, version_number=1)

    batch = _create_batch(db, project, owner)

    assert batch.form_project_id == project.id


def test_several_variants_with_an_identifier_open_a_batch(db, owner):
    project = _make_project(db, owner)
    _make_variant(db, project, owner, version_number=1, identifier_value="A")
    _make_variant(db, project, owner, version_number=2, identifier_value="B")

    batch = _create_batch(db, project, owner)

    assert batch.form_project_id == project.id


# ---------------------------------------------------------------------------
# Uploads into a batch opened while the project was ready
# ---------------------------------------------------------------------------


def _batch_then_not_ready(db, owner) -> SubmissionBatch:
    """A batch opened on a ready project (one variant), which then stops being
    ready: a second variant is published without VARIANT_IDENTIFIER."""
    project = _make_project(db, owner)
    _make_variant(db, project, owner, version_number=1)
    batch = _create_batch(db, project, owner)
    _make_variant(db, project, owner, version_number=2)
    return batch


def test_pdf_upload_is_refused_once_the_project_stops_being_ready(db, owner):
    batch = _batch_then_not_ready(db, owner)

    detail = _not_scan_ready(
        lambda: submission_batch_service.upload_pdf(
            db, batch.id, "late.pdf", _pdf_bytes(), owner
        )
    )

    assert detail["published_variant_count"] == 2
    assert _submission_count(db, batch) == 0
    assert _stored_files() == []


def test_image_upload_is_refused_once_the_project_stops_being_ready(db, owner):
    batch = _batch_then_not_ready(db, owner)

    detail = _not_scan_ready(
        lambda: submission_batch_service.upload_images(
            db, batch.id, [("late_pagina_1.png", _png_bytes())], owner
        )
    )

    assert detail["published_variant_count"] == 2
    assert _submission_count(db, batch) == 0
    assert _stored_files() == []


def test_uploads_into_a_ready_project_still_go_through(db, owner):
    """Control for the two refusals above: the same batch, still ready."""
    project = _make_project(db, owner)
    _make_variant(db, project, owner, version_number=1)
    batch = _create_batch(db, project, owner)

    submission_batch_service.upload_pdf(db, batch.id, "ok.pdf", _pdf_bytes(), owner)

    assert _submission_count(db, batch) == 1


# ---------------------------------------------------------------------------
# Order of the gates
# ---------------------------------------------------------------------------


def test_a_closed_project_keeps_the_active_gate_message(db, owner):
    project = _make_project(db, owner, status=FormProjectStatus.CLOSED)  # and not ready

    with pytest.raises(HTTPException) as refused:
        _create_batch(db, project, owner)

    assert refused.value.status_code == 409
    assert isinstance(refused.value.detail, str)
    assert "ACTIVE" in refused.value.detail


def test_a_non_member_is_denied_before_readiness(db, owner):
    project = _make_project(db, owner)  # not ready
    stranger = _make_user(db)

    with pytest.raises(HTTPException) as refused:
        _create_batch(db, project, stranger)

    assert refused.value.status_code == 403
