# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for closing a submission.

``POST /submissions/{id}/close``. What this suite pins down:

  * **the close** — SCANNED → REVIEWED, lock released, version bumped, one
    ``submission.closed`` audit event with ``unresolved_count``; the whole
    review page returned;
  * **unresolved readings are not written to** — they stay ``review_outcome``
    NULL, no review-log row, and count as left unresolved, a "Review anyway"
    placeholder included; a reading that never needed review is not counted;
  * **the guard** — not SCANNED (already closed included) → ``NOT_REVIEWABLE``
    with ``status``; a colleague's active lock → ``LOCK_LOST``; a stale version
    → ``SUBMISSION_CHANGED``; an expired lock does not hold it back; a refusal
    writes nothing;
  * **during a run** — refused with ``EXTRACTION_RUNNING`` while a run holds
    the batch, not held back by another shared change, and the batch is asked
    for before the submission's row;
  * **after the close** — a review save is refused, leaving the page releases
    nothing, the queue keeps the count of readings left unresolved while the
    batch list counts the submission closed and leaves them out, closing the
    last open submission makes the batch ready for export, an unresolvable
    one beside it included, and a later threshold change leaves the closed
    submission's unresolved readings as they were;
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
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.api.v1.endpoints.submission_batches import (
    get_submission_batch,
    list_submission_batches,
    update_batch_confidence_threshold,
)
from app.api.v1.endpoints.submissions import (
    close_submission,
    release_submission_lock,
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
from app.models.submission_review import SubmissionReview
from app.models.user import User
from app.repositories import submission_batch_repo
from app.schemas.submission import (
    ConfidenceThresholdUpdate,
    SubmissionCloseRequest,
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
        email=f"close-{uuid.uuid4().hex}@example.com",
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
        name="Close project",
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
    placeholder: bool = False,
    y: float = 10,
) -> SubmissionObjectResult:
    """A CODE reading on *sheet*: in review unless told otherwise, unresolved
    unless *resolved_by* confirmed it; a "Review anyway" placeholder (no
    payload) with *placeholder*."""
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
        detected_payload_json=None if placeholder else CODE_PAYLOAD,
        confidence_score=None if placeholder else 0.5,
        validity_forces_review=placeholder,
        review_required=review_required,
    )
    if resolved_by is not None:
        result.review_outcome = ReviewOutcome.CONFIRMED
        result.reviewed_by = resolved_by.id
        result.reviewed_at = NOW
    db.add(result)
    db.flush()
    return result


def _close(db, submission, user, *, version: int | None = None) -> SubmissionReviewDetail:
    return close_submission(
        submission_id=submission.id,
        data=SubmissionCloseRequest(
            review_version=submission.review_version if version is None else version
        ),
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


def _closed_events(db, submission) -> list[AuditLog]:
    return list(
        db.scalars(
            select(AuditLog).where(
                AuditLog.entity_id == str(submission.id),
                AuditLog.action == audit_service.SUBMISSION_CLOSED,
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
def _held_elsewhere(engine, batch_id: uuid.UUID, *, shared: bool) -> Iterator[None]:
    """Hold the batch's lock from another database session, as a run (or, with
    *shared*, another change) in progress would — the pattern of
    ``test_batch_operation_lock_integration``."""
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


# ---------------------------------------------------------------------------
# The close
# ---------------------------------------------------------------------------


def test_close_with_nothing_pending(db, actor, submission_and_sheet, frozen_now):
    submission, sheet = submission_and_sheet
    _reading(db, sheet, resolved_by=actor)
    submission.locked_by = actor.id
    submission.locked_at = NOW - timedelta(minutes=1)
    db.flush()

    detail = _close(db, submission, actor)

    assert detail.status is SubmissionStatus.REVIEWED
    assert detail.lock is None
    assert detail.review_version == 1
    assert detail.pending_review_count == 0
    closed = _fresh(db, Submission, submission.id)
    assert closed.status is SubmissionStatus.REVIEWED
    assert (closed.locked_by, closed.locked_at) == (None, None)
    (logged,) = _closed_events(db, submission)
    assert logged.actor_user_id == actor.id
    assert logged.metadata_json == {"unresolved_count": 0, "review_version": 1}


def test_close_leaves_unresolved_readings_unresolved(db, actor, submission_and_sheet, frozen_now):
    submission, sheet = submission_and_sheet
    unresolved = _reading(db, sheet, y=10)
    placeholder = _reading(db, sheet, placeholder=True, y=20)
    _reading(db, sheet, resolved_by=actor, y=30)
    # Never needed a human: not left unresolved either.
    _reading(db, sheet, review_required=False, y=40)

    detail = _close(db, submission, actor)

    assert detail.status is SubmissionStatus.REVIEWED
    assert (detail.review_count, detail.pending_review_count) == (3, 2)
    for result in (unresolved, placeholder):
        fresh = _fresh(db, SubmissionObjectResult, result.id)
        assert (fresh.review_outcome, fresh.reviewed_by, fresh.reviewed_at) == (None, None, None)
    assert db.scalar(select(func.count(SubmissionReview.id))) == 0
    (logged,) = _closed_events(db, submission)
    assert logged.metadata_json["unresolved_count"] == 2


def test_close_takes_an_expired_lock(db, actor, submission_and_sheet, frozen_now):
    submission, _sheet = submission_and_sheet
    luca = _make_user(db, "Luca", "Bianchi")
    submission.locked_by = luca.id
    submission.locked_at = NOW - timedelta(hours=1)
    db.flush()

    detail = _close(db, submission, actor)

    assert detail.status is SubmissionStatus.REVIEWED
    assert _fresh(db, Submission, submission.id).locked_by is None


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        SubmissionStatus.UPLOADED,
        SubmissionStatus.UNASSIGNED,
        SubmissionStatus.MATCHED,
        SubmissionStatus.MATCH_FAILED,
        SubmissionStatus.UNRESOLVABLE,
        SubmissionStatus.REVIEWED,
        SubmissionStatus.FINALIZED,
    ],
)
def test_close_outside_scanned_is_not_reviewable(
    db, actor, batch, template_page, frozen_now, status
):
    submission, _sheet = _make_submission(db, batch, template_page, status=status)

    exc = _refused(db, lambda: _close(db, submission, actor, version=0))

    assert exc.status_code == 409
    assert exc.detail["code"] == submission_lock_service.NOT_REVIEWABLE
    assert exc.detail["status"] == status.value
    fresh = _fresh(db, Submission, submission.id)
    assert (fresh.status, fresh.review_version) == (status, 0)
    assert _closed_events(db, submission) == []


def test_close_under_a_colleague_lock_is_lock_lost(db, actor, submission_and_sheet, frozen_now):
    submission, _sheet = submission_and_sheet
    luca = _make_user(db, "Luca", "Bianchi")
    submission.locked_by = luca.id
    submission.locked_at = NOW - timedelta(minutes=1)
    db.flush()

    exc = _refused(db, lambda: _close(db, submission, actor, version=0))

    assert exc.status_code == 409
    assert exc.detail["code"] == submission_lock_service.LOCK_LOST
    assert exc.detail["locked_by"]["display_name"] == "Luca Bianchi"
    fresh = _fresh(db, Submission, submission.id)
    assert (fresh.status, fresh.locked_by) == (SubmissionStatus.SCANNED, luca.id)
    assert _closed_events(db, submission) == []


def test_close_on_a_stale_version_is_submission_changed(db, actor, submission_and_sheet, frozen_now):
    submission, _sheet = submission_and_sheet
    submission.review_version = 4
    db.flush()

    exc = _refused(db, lambda: _close(db, submission, actor, version=3))

    assert exc.status_code == 409
    assert exc.detail["code"] == submission_lock_service.SUBMISSION_CHANGED
    assert exc.detail["current_version"] == 4
    fresh = _fresh(db, Submission, submission.id)
    assert (fresh.status, fresh.review_version) == (SubmissionStatus.SCANNED, 4)
    assert _closed_events(db, submission) == []


def test_closing_twice_is_not_reviewable(db, actor, submission_and_sheet, frozen_now):
    submission, _sheet = submission_and_sheet
    _close(db, submission, actor)

    exc = _refused(db, lambda: _close(db, submission, actor, version=1))

    assert exc.detail["code"] == submission_lock_service.NOT_REVIEWABLE
    assert exc.detail["status"] == SubmissionStatus.REVIEWED.value
    assert len(_closed_events(db, submission)) == 1


# ---------------------------------------------------------------------------
# During a run
# ---------------------------------------------------------------------------


def test_close_is_refused_while_a_run_is_in_progress(db, engine, actor, batch, submission_and_sheet, frozen_now):
    submission, _sheet = submission_and_sheet
    db.commit()

    with _held_elsewhere(engine, batch.id, shared=False):
        with pytest.raises(HTTPException) as excinfo:
            _close(db, submission, actor, version=0)
    db.rollback()

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["code"] == submission_lock_service.EXTRACTION_RUNNING
    fresh = _fresh(db, Submission, submission.id)
    assert (fresh.status, fresh.review_version) == (SubmissionStatus.SCANNED, 0)
    assert _closed_events(db, submission) == []


def test_close_is_not_held_back_by_another_shared_change(
    db, engine, actor, batch, submission_and_sheet, frozen_now
):
    submission, _sheet = submission_and_sheet

    with _held_elsewhere(engine, batch.id, shared=True):
        detail = _close(db, submission, actor)

    assert detail.status is SubmissionStatus.REVIEWED


def test_close_asks_for_the_batch_before_locking_the_submission(
    db, actor, submission_and_sheet, frozen_now, monkeypatch
):
    """A run holds the rows of the submissions it has walked until it commits:
    a close that locked the row first would wait out the run instead of being
    refused. Pinned by the order of the calls, as for "Review anyway"."""
    submission, _sheet = submission_and_sheet
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

    _close(db, submission, actor)

    assert calls == ["batch", "row"]


# ---------------------------------------------------------------------------
# After the close
# ---------------------------------------------------------------------------


def test_a_review_save_after_the_close_is_refused(db, actor, submission_and_sheet, frozen_now):
    submission, sheet = submission_and_sheet
    result = _reading(db, sheet)
    _close(db, submission, actor)

    exc = _refused(
        db,
        lambda: review_submission_result(
            submission_id=submission.id,
            result_id=result.id,
            data=SubmissionReviewActionRequest(action="CONFIRM", review_version=1),
            db=db,
            user=actor,
        ),
    )

    assert exc.detail["code"] == submission_lock_service.NOT_REVIEWABLE
    assert _fresh(db, SubmissionObjectResult, result.id).review_outcome is None


def test_leaving_the_page_after_the_close_changes_nothing(db, actor, submission_and_sheet, frozen_now):
    submission, _sheet = submission_and_sheet
    _close(db, submission, actor)

    release_submission_lock(submission_id=submission.id, db=db, user=actor)

    fresh = _fresh(db, Submission, submission.id)
    assert (fresh.status, fresh.locked_by, fresh.review_version) == (
        SubmissionStatus.REVIEWED, None, 1
    )


def test_the_queue_keeps_the_unresolved_count_of_a_closed_submission(
    db, actor, batch, submission_and_sheet, frozen_now
):
    submission, sheet = submission_and_sheet
    _reading(db, sheet, y=10)
    _reading(db, sheet, y=20)

    _close(db, submission, actor)

    row = _queue_row(db, batch, submission, actor)
    assert (row.status, row.pending_review_count) == (SubmissionStatus.REVIEWED, 2)
    listed = _listed(db, batch, actor)
    # The batch-level count measures work left: a closed submission has none.
    assert (listed.reviewed_count, listed.pending_review_count) == (1, 0)


def test_a_threshold_change_after_the_close_leaves_the_submission_alone(
    db, actor, batch, submission_and_sheet, frozen_now
):
    """The readings left unresolved at closing stay in review whatever the
    threshold becomes — lowering it must not turn them into automatic
    readings, nor change the count left at closing."""
    submission, sheet = submission_and_sheet
    unresolved = _reading(db, sheet)  # confidence 0.5, in review
    _close(db, submission, actor)

    update_batch_confidence_threshold(
        batch_id=batch.id,
        data=ConfidenceThresholdUpdate(confidence_threshold=0.1),
        db=db,
        user=actor,
    )

    assert _fresh(db, SubmissionObjectResult, unresolved.id).review_required is True
    assert _queue_row(db, batch, submission, actor).pending_review_count == 1


def test_closing_the_last_open_submission_makes_the_batch_ready(
    db, actor, batch, template_page, submission_and_sheet, frozen_now
):
    first, _ = submission_and_sheet
    second, _ = _make_submission(db, batch, template_page)
    _make_submission(db, batch, template_page, status=SubmissionStatus.UNRESOLVABLE)

    _close(db, first, actor)
    assert _listed(db, batch, actor).ready_for_export is False

    _close(db, second, actor)
    listed = _listed(db, batch, actor)
    assert (listed.reviewed_count, listed.unresolvable_count) == (2, 1)
    assert listed.ready_for_export is True


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


def _populated(db, batch, template_page, actor, n: int) -> Submission:
    submission, sheet = _make_submission(db, batch, template_page)
    for i in range(n):
        _reading(db, sheet, resolved_by=actor if i % 2 else None, y=10 + i)
    return submission


def test_a_close_costs_the_same_however_many_results(db, actor, batch, template_page, frozen_now):
    # Both with a resolved reading: the page read loads its reviewers in one
    # extra statement only when there are any.
    small = _populated(db, batch, template_page, actor, 2)
    large = _populated(db, batch, template_page, actor, 12)

    small_count, small_detail = _count(db, lambda: _close(db, small, actor, version=0))
    large_count, large_detail = _count(db, lambda: _close(db, large, actor, version=0))

    assert (small_detail.pending_review_count, large_detail.pending_review_count) == (1, 6)
    assert small_count == large_count
