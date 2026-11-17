# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for the per-batch operation lock.

What this suite pins down: while an extraction run holds its batch, a second
run, a "Review anyway" on one of its submissions and a change of its confidence
threshold are refused with the structured 409 ``EXTRACTION_RUNNING`` and write
nothing; while a "Review anyway" or a threshold change holds the batch
(shared), a run is refused, the other two are not.

"Elsewhere" is a SECOND connection to the test database, standing for another
request: the advisory lock belongs to a database session, so the test session
cannot hold it against itself. Advisory locks are not data, so the second
connection writes nothing that needs cleaning up; it is rolled back, which
releases the lock.

Skipped without TEST_DATABASE_URL, like the rest of tests/integration.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1.endpoints.submission_batches import (
    run_batch_extraction,
    update_batch_confidence_threshold,
)
from app.api.v1.endpoints.submissions import review_submission_anyway
from app.models.audit_log import AuditLog
from app.models.enums import (
    FormProjectStatus,
    FormProjectType,
    FormVariantStatus,
    PageObjectType,
    ProjectRole,
    SubmissionStatus,
    UserRole,
)
from app.models.form_project import FormProject
from app.models.form_project_member import FormProjectMember
from app.models.form_variant import FormVariant
from app.models.form_variant_page import FormVariantPage
from app.models.page_object import PageObject
from app.models.submission import Submission
from app.models.submission_batch import SubmissionBatch
from app.models.submission_object_result import SubmissionObjectResult
from app.models.submission_page import SubmissionPage
from app.models.user import User
from app.repositories import submission_batch_repo
from app.schemas.submission import ConfidenceThresholdUpdate
from app.services import audit_service, batch_operation_lock
from app.services.submission_lock_service import EXTRACTION_RUNNING

ATTEMPTED = datetime(2026, 10, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


@pytest.fixture
def actor(db) -> User:
    u = User(
        email=f"batchlock-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.USER,
    )
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def batch(db, actor) -> SubmissionBatch:
    project = FormProject(
        owner_id=actor.id,
        type=FormProjectType.EXAM,
        name="Batch-lock project",
        status=FormProjectStatus.ACTIVE,
    )
    db.add(project)
    db.flush()
    db.add(FormProjectMember(project_id=project.id, user_id=actor.id, role=ProjectRole.OWNER))
    batch = SubmissionBatch(
        form_project_id=project.id, name="Batch", created_by_user_id=actor.id
    )
    db.add(batch)
    db.flush()
    return batch


@pytest.fixture
def unread_submission(db, batch, actor) -> Submission:
    """A MATCHED submission a run has examined, with one QUESTION it could not
    read: what "Review anyway" applies to."""
    variant = FormVariant(
        form_project_id=batch.form_project_id,
        root_key=f"root-{uuid.uuid4().hex[:8]}",
        name="Variant",
        status=FormVariantStatus.PUBLISHED,
        version_number=1,
        created_by_user_id=actor.id,
    )
    db.add(variant)
    db.flush()
    template_page = FormVariantPage(form_variant_id=variant.id, page_index=1)
    db.add(template_page)
    db.flush()
    db.add(
        PageObject(
            form_variant_page_id=template_page.id,
            object_type=PageObjectType.QUESTION,
            x=10,
            y=10,
            width=50,
            height=50,
        )
    )
    submission = Submission(
        submission_batch_id=batch.id,
        matched_form_variant_id=variant.id,
        status=SubmissionStatus.MATCHED,
        extraction_attempted_at=ATTEMPTED,
    )
    db.add(submission)
    db.flush()
    db.add(
        SubmissionPage(
            submission_id=submission.id,
            form_variant_page_id=template_page.id,
            source_file_path=f"/tmp/{uuid.uuid4().hex}.png",
            source_filename="page0.png",
            source_page_order=0,
        )
    )
    db.flush()
    db.refresh(submission)
    return submission


@contextmanager
def _held_elsewhere(engine, batch_id: uuid.UUID, *, shared: bool) -> Iterator[None]:
    """Hold the batch's lock from another database session, as a run (or, with
    *shared*, a "Review anyway" or a threshold change) in progress would."""
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        if shared:
            batch_operation_lock.acquire_shared(session, batch_id)
        else:
            batch_operation_lock.acquire_for_run(session, batch_id)
        yield
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def _assert_extraction_running(excinfo) -> None:
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["code"] == EXTRACTION_RUNNING
    assert excinfo.value.detail["message"]


def _audit_actions(db, entity_id) -> list[str]:
    return list(
        db.scalars(select(AuditLog.action).where(AuditLog.entity_id == str(entity_id)))
    )


def _run(db, batch, actor):
    return run_batch_extraction(batch_id=batch.id, db=db, user=actor)


def _review_anyway(db, submission, actor):
    return review_submission_anyway(submission_id=submission.id, db=db, user=actor)


def _set_threshold(db, batch, actor, value):
    return update_batch_confidence_threshold(
        batch_id=batch.id,
        data=ConfidenceThresholdUpdate(confidence_threshold=value),
        db=db,
        user=actor,
    )


# ---------------------------------------------------------------------------
# While a run holds the batch
# ---------------------------------------------------------------------------


def test_a_second_run_is_refused_while_a_run_is_in_progress(db, engine, batch, actor):
    with _held_elsewhere(engine, batch.id, shared=False):
        with pytest.raises(HTTPException) as excinfo:
            _run(db, batch, actor)

    _assert_extraction_running(excinfo)
    assert audit_service.SUBMISSION_BATCH_EXTRACTION_RUN not in _audit_actions(db, batch.id)


def test_review_anyway_is_refused_while_a_run_is_in_progress(
    db, engine, batch, actor, unread_submission
):
    with _held_elsewhere(engine, batch.id, shared=False):
        with pytest.raises(HTTPException) as excinfo:
            _review_anyway(db, unread_submission, actor)

    _assert_extraction_running(excinfo)
    db.expire_all()
    assert db.get(Submission, unread_submission.id).status is SubmissionStatus.MATCHED
    placeholders = db.scalars(
        select(SubmissionObjectResult)
        .join(SubmissionPage)
        .where(SubmissionPage.submission_id == unread_submission.id)
    )
    assert list(placeholders) == []


def test_a_threshold_change_is_refused_while_a_run_is_in_progress(
    db, engine, batch, actor
):
    with _held_elsewhere(engine, batch.id, shared=False):
        with pytest.raises(HTTPException) as excinfo:
            _set_threshold(db, batch, actor, 0.9)

    _assert_extraction_running(excinfo)
    db.expire_all()
    assert db.get(SubmissionBatch, batch.id).confidence_threshold is None


# ---------------------------------------------------------------------------
# While a "Review anyway" or a threshold change holds the batch (shared)
# ---------------------------------------------------------------------------


def test_a_run_is_refused_while_another_change_holds_the_batch(db, engine, batch, actor):
    with _held_elsewhere(engine, batch.id, shared=True):
        with pytest.raises(HTTPException) as excinfo:
            _run(db, batch, actor)

    _assert_extraction_running(excinfo)


def test_review_anyway_is_not_held_back_by_another_shared_change(
    db, engine, batch, actor, unread_submission
):
    with _held_elsewhere(engine, batch.id, shared=True):
        result = _review_anyway(db, unread_submission, actor)

    assert result.status is SubmissionStatus.SCANNED
    assert result.placeholders_created == 1


def test_a_threshold_change_is_not_held_back_by_another_shared_change(
    db, engine, batch, actor
):
    with _held_elsewhere(engine, batch.id, shared=True):
        read = _set_threshold(db, batch, actor, 0.9)

    assert read.confidence_threshold == 0.9


def test_review_anyway_asks_for_the_batch_before_locking_the_submission(
    db, batch, actor, unread_submission, monkeypatch
):
    """A run holds the rows of the submissions it has walked until it commits:
    a "Review anyway" that locked the submission's row first would wait there
    for minutes instead of being refused. So the batch first, then the row.
    Pinned by the order of the calls, since the test session cannot hold a
    row against itself."""
    calls: list[str] = []
    real_acquire = batch_operation_lock.acquire_shared
    real_row_lock = submission_batch_repo.get_submission_for_update

    def acquire(db_, batch_id):
        calls.append("batch")
        return real_acquire(db_, batch_id)

    def row_lock(db_, submission_id):
        calls.append("row")
        return real_row_lock(db_, submission_id)

    monkeypatch.setattr(batch_operation_lock, "acquire_shared", acquire)
    monkeypatch.setattr(submission_batch_repo, "get_submission_for_update", row_lock)

    _review_anyway(db, unread_submission, actor)

    assert calls == ["batch", "row"]


def test_the_lock_is_per_batch(db, engine, batch, actor):
    """A run on ANOTHER batch does not hold this one."""
    with _held_elsewhere(engine, uuid.uuid4(), shared=False):
        summary = _run(db, batch, actor)

    assert summary.submissions_processed == 0
