# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for reopening a submission.

``POST /submissions/{id}/reopen``. What this suite pins down:

  * **the reopen** — REVIEWED → SCANNED, by any project member, the lock the
    reopener's, version bumped, ``total_score`` cleared, every ruling kept, one
    ``submission.reopened`` audit event with ``unresolved_count``; the whole
    review page returned;
  * **after the reopen** — a review save and a new close succeed, the queue and
    the batch list count it as work again, a batch ready for export is no
    longer ready;
  * **the refusals** — anything but REVIEWED (a second reopen included) →
    ``INVALID_STATUS`` with ``status``; a stale version → ``SUBMISSION_CHANGED``;
    a run in progress → ``EXTRACTION_RUNNING``; a refusal writes nothing; the
    batch lock, then the batch row, then the submission's row;
  * **the threshold** — a change made while the submission was closed reaches
    its unresolved readings on reopening, in both directions, and spares the
    reviewed ones and the other closed submissions; a change after the reopen
    reaches it as any open submission;
  * **statement count** — the same however many results the submission holds.

Skipped without TEST_DATABASE_URL, like the rest of tests/integration.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.api.v1.endpoints.submission_batches import (
    get_submission_batch,
    list_submission_batches,
    update_batch_confidence_threshold,
)
from app.api.v1.endpoints.submissions import (
    close_submission,
    reopen_submission,
    review_submission_result,
)
from app.models.audit_log import AuditLog
from app.models.enums import (
    FormProjectStatus,
    FormProjectType,
    FormVariantStatus,
    PageObjectType,
    PageOrderSource,
    ProjectRole,
    ReviewOutcome,
    SubmissionResultType,
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
from app.schemas.submission import (
    ConfidenceThresholdUpdate,
    SubmissionCloseRequest,
    SubmissionReopenRequest,
    SubmissionReviewActionRequest,
    SubmissionReviewDetail,
)
from app.services import audit_service, batch_operation_lock, submission_lock_service

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)
CODE_PAYLOAD = {"decoded_value": "ABC", "decode_successful": True}


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_user(db, first: str, last: str) -> User:
    u = User(
        email=f"reopen-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.USER,
        first_name=first,
        last_name=last,
    )
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def frozen_now(monkeypatch) -> datetime:
    monkeypatch.setattr(submission_lock_service, "_now", lambda: NOW)
    return NOW


@pytest.fixture
def actor(db) -> User:
    return _make_user(db, "Mario", "Rossi")


@pytest.fixture
def batch(db, actor) -> SubmissionBatch:
    project = FormProject(
        owner_id=actor.id,
        type=FormProjectType.EXAM,
        name="Reopen project",
        status=FormProjectStatus.ACTIVE,
    )
    db.add(project)
    db.flush()
    db.add(FormProjectMember(project_id=project.id, user_id=actor.id, role=ProjectRole.OWNER))
    # A threshold of its own: the tests move it explicitly.
    batch = SubmissionBatch(
        form_project_id=project.id,
        name="Batch",
        created_by_user_id=actor.id,
        confidence_threshold=0.8,
    )
    db.add(batch)
    db.flush()
    return batch


@pytest.fixture
def colleague(db, batch) -> User:
    luca = _make_user(db, "Luca", "Bianchi")
    db.add(FormProjectMember(project_id=batch.form_project_id, user_id=luca.id, role=ProjectRole.COLLABORATOR))
    db.flush()
    return luca


@pytest.fixture
def template_page(db, batch, actor) -> FormVariantPage:
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
    page = FormVariantPage(form_variant_id=variant.id, page_index=1, width_px=794, height_px=1123)
    db.add(page)
    db.flush()
    return page


def _make_submission(
    db, batch, template_page, *, status=SubmissionStatus.SCANNED
) -> tuple[Submission, SubmissionPage]:
    submission = Submission(
        submission_batch_id=batch.id,
        matched_form_variant_id=template_page.form_variant_id,
        status=status,
    )
    db.add(submission)
    db.flush()
    sheet = SubmissionPage(
        submission_id=submission.id,
        source_file_path=f"/tmp/{uuid.uuid4().hex}.png",
        source_filename="page.png",
        source_page_order=0,
        page_order_source=PageOrderSource.CODE,
        form_variant_page_id=template_page.id,
        match_confidence=1.0,
    )
    db.add(sheet)
    db.flush()
    return submission, sheet


@pytest.fixture
def submission_and_sheet(db, batch, template_page) -> tuple[Submission, SubmissionPage]:
    return _make_submission(db, batch, template_page)


def _reading(
    db,
    sheet: SubmissionPage,
    *,
    resolved_by: User | None = None,
    review_required: bool = True,
    y: float = 10,
) -> SubmissionObjectResult:
    """A CODE reading on *sheet* read with confidence 0.5: in review unless
    told otherwise, unresolved unless *resolved_by* confirmed it."""
    obj = PageObject(
        form_variant_page_id=sheet.form_variant_page_id,
        object_type=PageObjectType.CODE,
        x=10, y=y, width=200, height=80,
    )
    db.add(obj)
    db.flush()
    result = SubmissionObjectResult(
        submission_page_id=sheet.id,
        page_object_id=obj.id,
        result_type=SubmissionResultType.CODE,
        detected_payload_json=CODE_PAYLOAD,
        confidence_score=0.5,
        validity_forces_review=False,
        review_required=review_required,
    )
    if resolved_by is not None:
        result.review_outcome = ReviewOutcome.CONFIRMED
        result.reviewed_by = resolved_by.id
        result.reviewed_at = NOW
    db.add(result)
    db.flush()
    return result


def _close(db, submission, user) -> SubmissionReviewDetail:
    db.refresh(submission)
    return close_submission(
        submission_id=submission.id,
        data=SubmissionCloseRequest(review_version=submission.review_version),
        db=db,
        user=user,
    )


def _reopen(db, submission, user, *, version: int | None = None) -> SubmissionReviewDetail:
    if version is None:
        db.refresh(submission)
        version = submission.review_version
    return reopen_submission(
        submission_id=submission.id,
        data=SubmissionReopenRequest(review_version=version),
        db=db,
        user=user,
    )


def _set_threshold(db, batch, user, value: float) -> None:
    update_batch_confidence_threshold(
        batch_id=batch.id,
        data=ConfidenceThresholdUpdate(confidence_threshold=value),
        db=db,
        user=user,
    )


def _refused(db, call) -> HTTPException:
    """Run a call expected to fail; roll back as ``get_db`` would on error.

    The setup is committed first (to the per-test SAVEPOINT, see conftest), so
    the rollback discards exactly what the refused call wrote."""
    db.commit()
    with pytest.raises(HTTPException) as excinfo:
        call()
    db.rollback()
    return excinfo.value


def _fresh(db, model, id_):
    db.expire_all()
    return db.get(model, id_)


def _events(db, submission, action: str) -> list[AuditLog]:
    return list(
        db.scalars(
            select(AuditLog).where(
                AuditLog.entity_id == str(submission.id),
                AuditLog.action == action,
            )
        )
    )


def _queue_row(db, batch, submission, user):
    detail = get_submission_batch(batch_id=batch.id, db=db, user=user)
    return next(s for s in detail.submissions if s.id == submission.id)


def _listed(db, batch, user):
    items = list_submission_batches(project_id=batch.form_project_id, db=db, user=user)
    return next(b for b in items if b.id == batch.id)


@contextmanager
def _held_elsewhere(engine, batch_id: uuid.UUID) -> Iterator[None]:
    """Hold the batch's lock from another database session, as a run in
    progress would — the pattern of ``test_batch_operation_lock_integration``."""
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        batch_operation_lock.acquire_for_run(session, batch_id)
        yield
    finally:
        session.close()
        transaction.rollback()
        connection.close()


# ---------------------------------------------------------------------------
# The reopen
# ---------------------------------------------------------------------------


def test_a_colleague_reopens_a_closed_submission(db, actor, colleague, submission_and_sheet, frozen_now):
    submission, sheet = submission_and_sheet
    unresolved = _reading(db, sheet, y=10)
    confirmed = _reading(db, sheet, resolved_by=actor, y=20)
    _close(db, submission, actor)
    submission.total_score = 7.5
    db.flush()

    detail = _reopen(db, submission, colleague)

    assert detail.status is SubmissionStatus.SCANNED
    assert detail.lock is not None and detail.lock.user_id == colleague.id
    assert detail.review_version == 2
    assert (detail.review_count, detail.pending_review_count) == (2, 1)
    reopened = _fresh(db, Submission, submission.id)
    assert (reopened.status, reopened.locked_by, reopened.locked_at) == (
        SubmissionStatus.SCANNED, colleague.id, NOW
    )
    assert reopened.total_score is None
    kept = _fresh(db, SubmissionObjectResult, confirmed.id)
    assert (kept.review_outcome, kept.reviewed_by) == (ReviewOutcome.CONFIRMED, actor.id)
    assert _fresh(db, SubmissionObjectResult, unresolved.id).review_outcome is None
    (logged,) = _events(db, submission, audit_service.SUBMISSION_REOPENED)
    assert logged.actor_user_id == colleague.id
    assert logged.metadata_json == {"unresolved_count": 1, "review_version": 2}


def test_after_the_reopen_review_and_close_work_again(db, actor, submission_and_sheet, frozen_now):
    submission, sheet = submission_and_sheet
    pending = _reading(db, sheet)
    _close(db, submission, actor)
    _reopen(db, submission, actor)

    detail = review_submission_result(
        submission_id=submission.id,
        result_id=pending.id,
        data=SubmissionReviewActionRequest(action="CONFIRM", review_version=2),
        db=db,
        user=actor,
    )
    assert detail.pending_review_count == 0

    closed = _close(db, submission, actor)

    assert (closed.status, closed.review_version) == (SubmissionStatus.REVIEWED, 4)
    assert len(_events(db, submission, audit_service.SUBMISSION_CLOSED)) == 2
    second_close = max(
        _events(db, submission, audit_service.SUBMISSION_CLOSED),
        key=lambda e: e.metadata_json["review_version"],
    )
    assert second_close.metadata_json["unresolved_count"] == 0


def test_a_reopened_submission_is_work_again_in_the_queue(
    db, actor, batch, submission_and_sheet, frozen_now
):
    submission, sheet = submission_and_sheet
    _reading(db, sheet)
    _close(db, submission, actor)
    assert _listed(db, batch, actor).ready_for_export is True

    _reopen(db, submission, actor)

    row = _queue_row(db, batch, submission, actor)
    assert (row.status, row.pending_review_count) == (SubmissionStatus.SCANNED, 1)
    listed = _listed(db, batch, actor)
    assert (listed.reviewed_count, listed.pending_review_count) == (0, 1)
    assert listed.ready_for_export is False


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        SubmissionStatus.SCANNED,
        SubmissionStatus.MATCHED,
        SubmissionStatus.FINALIZED,
        SubmissionStatus.UNRESOLVABLE,
    ],
)
def test_reopen_outside_reviewed_is_an_invalid_status(
    db, actor, batch, template_page, frozen_now, status
):
    submission, _sheet = _make_submission(db, batch, template_page, status=status)

    exc = _refused(db, lambda: _reopen(db, submission, actor, version=0))

    assert exc.status_code == 409
    assert exc.detail["code"] == submission_lock_service.INVALID_STATUS
    assert exc.detail["status"] == status.value
    fresh = _fresh(db, Submission, submission.id)
    assert (fresh.status, fresh.review_version, fresh.locked_by) == (status, 0, None)
    assert _events(db, submission, audit_service.SUBMISSION_REOPENED) == []


def test_reopening_twice_is_an_invalid_status(db, actor, colleague, submission_and_sheet, frozen_now):
    submission, _sheet = submission_and_sheet
    _close(db, submission, actor)
    _reopen(db, submission, actor)

    # The colleague's screen still shows the closed submission, version 1.
    exc = _refused(db, lambda: _reopen(db, submission, colleague, version=1))

    assert exc.detail["code"] == submission_lock_service.INVALID_STATUS
    assert exc.detail["status"] == SubmissionStatus.SCANNED.value
    assert exc.detail["message"] == "This submission has already been reopened."
    fresh = _fresh(db, Submission, submission.id)
    assert (fresh.locked_by, fresh.review_version) == (actor.id, 2)
    assert len(_events(db, submission, audit_service.SUBMISSION_REOPENED)) == 1


def test_reopen_on_a_stale_version_is_submission_changed(db, actor, submission_and_sheet, frozen_now):
    submission, _sheet = submission_and_sheet
    _close(db, submission, actor)

    exc = _refused(db, lambda: _reopen(db, submission, actor, version=0))

    assert exc.status_code == 409
    assert exc.detail["code"] == submission_lock_service.SUBMISSION_CHANGED
    assert exc.detail["current_version"] == 1
    fresh = _fresh(db, Submission, submission.id)
    assert (fresh.status, fresh.review_version, fresh.locked_by) == (
        SubmissionStatus.REVIEWED, 1, None
    )
    assert _events(db, submission, audit_service.SUBMISSION_REOPENED) == []


def test_reopen_is_refused_while_a_run_is_in_progress(
    db, engine, actor, batch, template_page, frozen_now
):
    # Built closed rather than closed through the API: the close would hold the
    # batch lock (shared) in this test's transaction, and the "run" below could
    # not take it.
    submission, _sheet = _make_submission(
        db, batch, template_page, status=SubmissionStatus.REVIEWED
    )
    submission.review_version = 1
    db.commit()

    with _held_elsewhere(engine, batch.id):
        with pytest.raises(HTTPException) as excinfo:
            _reopen(db, submission, actor, version=1)
    db.rollback()

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["code"] == submission_lock_service.EXTRACTION_RUNNING
    fresh = _fresh(db, Submission, submission.id)
    assert (fresh.status, fresh.review_version) == (SubmissionStatus.REVIEWED, 1)
    assert _events(db, submission, audit_service.SUBMISSION_REOPENED) == []


def test_reopen_locks_the_batch_then_its_row_then_the_submission(
    db, actor, submission_and_sheet, frozen_now, monkeypatch
):
    """The batch lock first, as for the close; then the batch row, which a
    threshold change writes before reading the results; then the submission's
    row."""
    submission, _sheet = submission_and_sheet
    _close(db, submission, actor)
    calls: list[str] = []
    real_acquire = batch_operation_lock.acquire_shared
    real_batch_row = submission_batch_repo.get_for_update
    real_row = submission_batch_repo.get_submission_for_update

    def acquire(db_, batch_id):
        calls.append("batch")
        return real_acquire(db_, batch_id)

    def batch_row(db_, batch_id):
        calls.append("batch-row")
        return real_batch_row(db_, batch_id)

    def row(db_, submission_id):
        calls.append("row")
        return real_row(db_, submission_id)

    monkeypatch.setattr(batch_operation_lock, "acquire_shared", acquire)
    monkeypatch.setattr(submission_batch_repo, "get_for_update", batch_row)
    monkeypatch.setattr(submission_batch_repo, "get_submission_for_update", row)

    _reopen(db, submission, actor)

    assert calls == ["batch", "batch-row", "row"]


# ---------------------------------------------------------------------------
# The threshold
# ---------------------------------------------------------------------------


def test_reopen_catches_up_with_a_threshold_lowered_while_closed(
    db, actor, batch, template_page, submission_and_sheet, frozen_now
):
    submission, sheet = submission_and_sheet
    unresolved = _reading(db, sheet, y=10)  # 0.5 < 0.8: in review
    confirmed = _reading(db, sheet, resolved_by=actor, y=20)
    other, other_sheet = _make_submission(db, batch, template_page)
    other_unresolved = _reading(db, other_sheet)
    _close(db, submission, actor)
    _close(db, other, actor)
    _set_threshold(db, batch, actor, 0.3)
    assert _fresh(db, SubmissionObjectResult, unresolved.id).review_required is True

    detail = _reopen(db, submission, actor)

    assert detail.pending_review_count == 0
    assert _fresh(db, SubmissionObjectResult, unresolved.id).review_required is False
    # A ruling keeps the flag it had when it was made.
    assert _fresh(db, SubmissionObjectResult, confirmed.id).review_required is True
    # The other submission is still closed: left as it was at closing.
    assert _fresh(db, SubmissionObjectResult, other_unresolved.id).review_required is True
    (logged,) = _events(db, submission, audit_service.SUBMISSION_REOPENED)
    assert logged.metadata_json["unresolved_count"] == 0


def test_reopen_catches_up_with_a_threshold_raised_while_closed(
    db, actor, batch, submission_and_sheet, frozen_now
):
    submission, sheet = submission_and_sheet
    _set_threshold(db, batch, actor, 0.3)
    automatic = _reading(db, sheet, review_required=False)  # 0.5 ≥ 0.3
    _close(db, submission, actor)
    _set_threshold(db, batch, actor, 0.9)

    detail = _reopen(db, submission, actor)

    assert _fresh(db, SubmissionObjectResult, automatic.id).review_required is True
    assert detail.pending_review_count == 1


def test_a_threshold_change_after_the_reopen_reaches_the_submission(
    db, actor, batch, submission_and_sheet, frozen_now
):
    submission, sheet = submission_and_sheet
    unresolved = _reading(db, sheet)
    _close(db, submission, actor)
    _reopen(db, submission, actor)

    _set_threshold(db, batch, actor, 0.3)

    assert _fresh(db, SubmissionObjectResult, unresolved.id).review_required is False
    assert _queue_row(db, batch, submission, actor).pending_review_count == 0


# ---------------------------------------------------------------------------
# Statement count
# ---------------------------------------------------------------------------


def _count(db, call) -> tuple[int, object]:
    statements: list[str] = []

    def _record(conn, cursor, statement, *args):
        statements.append(statement)

    engine = db.get_bind().engine
    db.expire_all()
    event.listen(engine, "before_cursor_execute", _record)
    try:
        out = call()
    finally:
        event.remove(engine, "before_cursor_execute", _record)
    return len(statements), out


def _closed_with_readings(db, batch, template_page, actor, n: int) -> Submission:
    submission, sheet = _make_submission(db, batch, template_page)
    for i in range(n):
        _reading(db, sheet, resolved_by=actor if i % 2 else None, y=10 + i)
    _close(db, submission, actor)
    return submission


def test_a_reopen_costs_the_same_however_many_results(
    db, actor, batch, template_page, frozen_now
):
    # Both with a resolved reading (the page read loads its reviewers in one
    # extra statement only when there are any), and both with
    # unresolved readings the reopen recomputes: lowered while closed.
    small = _closed_with_readings(db, batch, template_page, actor, 2)
    large = _closed_with_readings(db, batch, template_page, actor, 12)
    _set_threshold(db, batch, actor, 0.3)

    small_count, small_detail = _count(db, lambda: _reopen(db, small, actor, version=1))
    large_count, large_detail = _count(db, lambda: _reopen(db, large, actor, version=1))

    assert (small_detail.pending_review_count, large_detail.pending_review_count) == (0, 0)
    assert small_count == large_count
