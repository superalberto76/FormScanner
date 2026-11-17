# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for the review soft lock and review_version.

Two mechanisms per submission, and what this suite pins down for each:

  * the SOFT LOCK — acquired on open unless a colleague actively holds it,
    taken over explicitly whoever holds it (audited only when it overrides an
    active colleague), released only by its own holder, expired after 15
    minutes of inactivity and then simply ignored, never cleaned up;
  * ``review_version`` — every admitted save bumps it, and a save declaring
    another version is refused;

plus ``guard_review_save``'s precedence (status, then lock, then version), its
refusal to commit on the caller's behalf, the structured 409 ``detail``
objects, authorisation (members only, CLOSED projects included) and
``display_name``.

Time is controlled by patching ``submission_lock_service._now``. Skipped
without TEST_DATABASE_URL, like the rest of tests/integration.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select

from app.api.v1.endpoints.submissions import (
    acquire_submission_lock,
    release_submission_lock,
    take_over_submission_lock,
)
from app.models.audit_log import AuditLog
from app.models.enums import (
    FormProjectStatus,
    FormProjectType,
    ProjectRole,
    SubmissionStatus,
    UserRole,
)
from app.models.form_project import FormProject
from app.models.form_project_member import FormProjectMember
from app.models.submission import Submission
from app.models.submission_batch import SubmissionBatch
from app.models.user import User
from app.services import audit_service, submission_lock_service as lock_service
from app.services.submission_lock_service import (
    LOCK_LOST,
    LOCK_TIMEOUT,
    NOT_REVIEWABLE,
    SUBMISSION_CHANGED,
)

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


class _Clock:
    """A settable stand-in for ``submission_lock_service._now``."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    c = _Clock(T0)
    monkeypatch.setattr(lock_service, "_now", c)
    return c


def _make_user(db, first: str | None = None, last: str | None = None) -> User:
    u = User(
        email=f"lock-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.USER,
        first_name=first,
        last_name=last,
    )
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def mario(db) -> User:
    return _make_user(db, "Mario", "Rossi")


@pytest.fixture
def luca(db) -> User:
    return _make_user(db, "Luca", "Bianchi")


@pytest.fixture
def project(db, mario, luca) -> FormProject:
    project = FormProject(
        owner_id=mario.id,
        type=FormProjectType.EXAM,
        name="Lock project",
        status=FormProjectStatus.ACTIVE,
    )
    db.add(project)
    db.flush()
    db.add(FormProjectMember(project_id=project.id, user_id=mario.id, role=ProjectRole.OWNER))
    db.add(
        FormProjectMember(
            project_id=project.id, user_id=luca.id, role=ProjectRole.COLLABORATOR
        )
    )
    db.flush()
    return project


@pytest.fixture
def submission(db, project, mario) -> Submission:
    batch = SubmissionBatch(
        form_project_id=project.id, name="Batch", created_by_user_id=mario.id
    )
    db.add(batch)
    db.flush()
    s = Submission(submission_batch_id=batch.id, status=SubmissionStatus.SCANNED)
    db.add(s)
    db.flush()
    return s


def _lock(submission: Submission, user: User, at: datetime) -> None:
    submission.locked_by = user.id
    submission.locked_at = at


def _conflict(excinfo, code: str) -> dict:
    """The structured detail of a 409, after checking its shape and code."""
    exc = excinfo.value
    assert exc.status_code == 409
    assert isinstance(exc.detail, dict)
    assert exc.detail["code"] == code
    assert isinstance(exc.detail["message"], str) and exc.detail["message"]
    return exc.detail


def _takeover_events(db, submission) -> list[AuditLog]:
    return list(
        db.scalars(
            select(AuditLog).where(
                AuditLog.action == audit_service.SUBMISSION_LOCK_TAKEN_OVER,
                AuditLog.entity_id == str(submission.id),
            )
        ).all()
    )


# ---------------------------------------------------------------------------
# acquire
# ---------------------------------------------------------------------------
def test_acquire_on_a_free_lock_takes_it(db, clock, submission, mario):
    state = lock_service.acquire(db, submission.id, mario)

    assert state.acquired is True
    assert state.lock.user_id == mario.id
    assert state.lock.display_name == "Mario Rossi"
    assert state.lock.locked_at == T0
    assert state.review_version == 0
    db.refresh(submission)
    assert (submission.locked_by, submission.locked_at) == (mario.id, T0)


def test_acquiring_my_own_lock_again_renews_it(db, clock, submission, mario):
    lock_service.acquire(db, submission.id, mario)
    clock.advance(minutes=10)

    state = lock_service.acquire(db, submission.id, mario)

    assert state.acquired is True
    assert state.lock.locked_at == T0 + timedelta(minutes=10)
    db.refresh(submission)
    assert submission.locked_at == T0 + timedelta(minutes=10)


def test_an_active_lock_of_another_user_is_not_acquired_and_reported(
    db, clock, submission, mario, luca
):
    lock_service.acquire(db, submission.id, mario)
    clock.advance(minutes=5)

    state = lock_service.acquire(db, submission.id, luca)

    assert state.acquired is False
    assert state.lock.user_id == mario.id
    assert state.lock.display_name == "Mario Rossi"
    assert state.lock.locked_at == T0
    db.refresh(submission)
    assert (submission.locked_by, submission.locked_at) == (mario.id, T0)


def test_an_expired_lock_of_another_user_is_acquired(
    db, clock, submission, mario, luca
):
    lock_service.acquire(db, submission.id, mario)
    clock.advance(minutes=16)

    state = lock_service.acquire(db, submission.id, luca)

    assert state.acquired is True
    assert state.lock.user_id == luca.id


def test_a_lock_expires_at_exactly_the_timeout(db, clock, submission, mario, luca):
    """The boundary, documented: active while strictly LESS than 15 minutes
    have passed since the last activity; at exactly 15 it has expired."""
    lock_service.acquire(db, submission.id, mario)

    clock.now = T0 + LOCK_TIMEOUT - timedelta(microseconds=1)
    assert lock_service.acquire(db, submission.id, luca).acquired is False

    clock.now = T0 + LOCK_TIMEOUT
    assert lock_service.acquire(db, submission.id, luca).acquired is True


@pytest.mark.parametrize(
    "status", [SubmissionStatus.MATCHED, SubmissionStatus.REVIEWED]
)
def test_acquire_outside_scanned_is_not_reviewable(
    db, clock, submission, mario, status
):
    submission.status = status
    db.flush()

    with pytest.raises(HTTPException) as excinfo:
        lock_service.acquire(db, submission.id, mario)

    detail = _conflict(excinfo, NOT_REVIEWABLE)
    assert detail["status"] == status.value
    db.refresh(submission)
    assert submission.locked_by is None


def test_acquire_reads_the_row_for_update(db, clock, submission, mario):
    """Two simultaneous opens must serialise on the row, not both acquire."""
    statements: list[str] = []

    def _record(conn, cursor, statement, *args):
        statements.append(statement)

    engine = db.get_bind().engine
    event.listen(engine, "before_cursor_execute", _record)
    try:
        lock_service.acquire(db, submission.id, mario)
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    reads = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert "FROM submissions" in reads[0]
    assert reads[0].rstrip().endswith("FOR UPDATE")


# ---------------------------------------------------------------------------
# take_over
# ---------------------------------------------------------------------------
def test_taking_over_an_active_lock_acquires_and_is_audited(
    db, clock, submission, mario, luca
):
    lock_service.acquire(db, submission.id, mario)
    clock.advance(minutes=3)

    state = lock_service.take_over(db, submission.id, luca)

    assert state.acquired is True
    assert state.lock.user_id == luca.id
    assert state.lock.locked_at == T0 + timedelta(minutes=3)
    (audit,) = _takeover_events(db, submission)
    assert audit.actor_user_id == luca.id
    assert audit.entity_type == "Submission"
    assert audit.metadata_json == {
        "previous_holder_id": str(mario.id),
        "previous_locked_at": T0.isoformat(),
    }


def test_taking_over_a_free_lock_is_a_plain_acquire(db, clock, submission, luca):
    state = lock_service.take_over(db, submission.id, luca)

    assert state.acquired is True
    assert state.lock.user_id == luca.id
    assert _takeover_events(db, submission) == []


def test_taking_over_an_expired_lock_is_not_audited(
    db, clock, submission, mario, luca
):
    lock_service.acquire(db, submission.id, mario)
    clock.advance(minutes=20)

    assert lock_service.take_over(db, submission.id, luca).acquired is True
    assert _takeover_events(db, submission) == []


def test_take_over_outside_scanned_is_not_reviewable(db, clock, submission, luca):
    submission.status = SubmissionStatus.MATCHED
    db.flush()

    with pytest.raises(HTTPException) as excinfo:
        lock_service.take_over(db, submission.id, luca)

    _conflict(excinfo, NOT_REVIEWABLE)


# ---------------------------------------------------------------------------
# release
# ---------------------------------------------------------------------------
def test_releasing_my_lock_clears_it(db, clock, submission, mario):
    lock_service.acquire(db, submission.id, mario)

    lock_service.release(db, submission.id, mario)

    db.refresh(submission)
    assert (submission.locked_by, submission.locked_at) == (None, None)


def test_releasing_another_users_lock_leaves_it_untouched(
    db, clock, submission, mario, luca
):
    lock_service.acquire(db, submission.id, mario)

    lock_service.release(db, submission.id, luca)  # no error

    db.refresh(submission)
    assert (submission.locked_by, submission.locked_at) == (mario.id, T0)


def test_releasing_with_no_lock_is_a_no_op(db, clock, submission, mario):
    lock_service.release(db, submission.id, mario)
    lock_service.release(db, submission.id, mario)

    db.refresh(submission)
    assert submission.locked_by is None


def test_release_is_allowed_in_any_status(db, clock, submission, mario):
    lock_service.acquire(db, submission.id, mario)
    submission.status = SubmissionStatus.REVIEWED
    db.flush()

    lock_service.release(db, submission.id, mario)

    db.refresh(submission)
    assert submission.locked_by is None


# ---------------------------------------------------------------------------
# guard_review_save
# ---------------------------------------------------------------------------
def test_a_save_on_a_free_lock_acquires_it_and_bumps_the_version(
    db, clock, submission, mario
):
    saved = lock_service.guard_review_save(db, submission.id, mario, 0)

    assert saved.id == submission.id
    assert saved.review_version == 1
    assert (saved.locked_by, saved.locked_at) == (mario.id, T0)


def test_a_save_under_my_own_lock_renews_it(db, clock, submission, mario):
    lock_service.acquire(db, submission.id, mario)
    clock.advance(minutes=12)

    saved = lock_service.guard_review_save(db, submission.id, mario, 0)

    assert saved.review_version == 1
    assert saved.locked_at == T0 + timedelta(minutes=12)


def test_a_save_under_another_users_active_lock_is_lock_lost(
    db, clock, submission, mario, luca
):
    lock_service.acquire(db, submission.id, luca)
    clock.advance(minutes=1)

    with pytest.raises(HTTPException) as excinfo:
        lock_service.guard_review_save(db, submission.id, mario, 0)

    detail = _conflict(excinfo, LOCK_LOST)
    assert detail["locked_by"] == {
        "user_id": str(luca.id),
        "display_name": "Luca Bianchi",
        "locked_at": T0.isoformat(),
    }
    db.refresh(submission)
    assert submission.review_version == 0
    assert submission.locked_by == luca.id


def test_a_save_under_another_users_expired_lock_acquires_it(
    db, clock, submission, mario, luca
):
    lock_service.acquire(db, submission.id, luca)
    clock.advance(minutes=16)

    saved = lock_service.guard_review_save(db, submission.id, mario, 0)

    assert saved.review_version == 1
    assert saved.locked_by == mario.id


def test_a_save_declaring_a_stale_version_is_submission_changed(
    db, clock, submission, mario
):
    lock_service.guard_review_save(db, submission.id, mario, 0)

    with pytest.raises(HTTPException) as excinfo:
        lock_service.guard_review_save(db, submission.id, mario, 0)

    detail = _conflict(excinfo, SUBMISSION_CHANGED)
    assert detail["current_version"] == 1
    db.refresh(submission)
    assert submission.review_version == 1


def test_lock_lost_wins_over_a_stale_version(db, clock, submission, mario, luca):
    lock_service.guard_review_save(db, submission.id, luca, 0)  # v1, Luca holds

    with pytest.raises(HTTPException) as excinfo:
        lock_service.guard_review_save(db, submission.id, mario, 0)

    _conflict(excinfo, LOCK_LOST)


@pytest.mark.parametrize(
    "status", [SubmissionStatus.MATCHED, SubmissionStatus.REVIEWED]
)
def test_a_save_outside_scanned_is_not_reviewable(
    db, clock, submission, mario, luca, status
):
    """Status is checked first, even ahead of a colleague's active lock."""
    _lock(submission, luca, T0)
    submission.status = status
    db.flush()

    with pytest.raises(HTTPException) as excinfo:
        lock_service.guard_review_save(db, submission.id, mario, 0)

    assert _conflict(excinfo, NOT_REVIEWABLE)["status"] == status.value


def test_the_guard_does_not_commit(db, clock, submission, mario):
    """The review action owns the transaction: rolling it back must undo the
    version bump and the lock the guard took along with it."""
    db.commit()  # the submission itself is durable from here on

    lock_service.guard_review_save(db, submission.id, mario, 0)
    db.rollback()

    db.refresh(submission)
    assert submission.review_version == 0
    assert submission.locked_by is None


# ---------------------------------------------------------------------------
# The two-reviewer scenario
# ---------------------------------------------------------------------------
def test_mario_and_luca(db, clock, submission, mario, luca):
    # Mario opens the submission at v0, then goes idle.
    state = lock_service.acquire(db, submission.id, mario)
    assert (state.acquired, state.review_version) == (True, 0)

    # 16 minutes later Luca finds it free, opens it and saves: v1.
    clock.advance(minutes=16)
    assert lock_service.acquire(db, submission.id, luca).acquired is True
    assert lock_service.guard_review_save(db, submission.id, luca, 0).review_version == 1

    # Mario comes back and saves from his v0 screen: Luca is active on it.
    with pytest.raises(HTTPException) as excinfo:
        lock_service.guard_review_save(db, submission.id, mario, 0)
    assert _conflict(excinfo, LOCK_LOST)["locked_by"]["user_id"] == str(luca.id)

    # 16 more idle minutes: Luca's lock expires, but Mario's screen is stale.
    clock.advance(minutes=16)
    with pytest.raises(HTTPException) as excinfo:
        lock_service.guard_review_save(db, submission.id, mario, 0)
    assert _conflict(excinfo, SUBMISSION_CHANGED)["current_version"] == 1

    # After reloading (v1), Mario's save goes through and the lock is his.
    saved = lock_service.guard_review_save(db, submission.id, mario, 1)
    assert saved.review_version == 2
    assert saved.locked_by == mario.id


# ---------------------------------------------------------------------------
# Authorisation and routes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "call",
    [
        lambda db, sid, u: lock_service.acquire(db, sid, u),
        lambda db, sid, u: lock_service.take_over(db, sid, u),
        lambda db, sid, u: lock_service.release(db, sid, u),
        lambda db, sid, u: lock_service.guard_review_save(db, sid, u, 0),
    ],
    ids=["acquire", "take_over", "release", "guard_review_save"],
)
def test_a_non_member_gets_the_usual_403(db, clock, submission, call):
    outsider = _make_user(db, "Out", "Sider")

    with pytest.raises(HTTPException) as excinfo:
        call(db, submission.id, outsider)

    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "You are not a member of this project"


def test_an_unknown_submission_is_404(db, clock, mario):
    with pytest.raises(HTTPException) as excinfo:
        lock_service.acquire(db, uuid.uuid4(), mario)

    assert excinfo.value.status_code == 404


@pytest.mark.parametrize(
    "project_status", [FormProjectStatus.CLOSED, FormProjectStatus.ARCHIVED]
)
def test_review_works_on_a_closed_project(
    db, clock, project, submission, mario, project_status
):
    project.status = project_status
    db.flush()

    assert lock_service.acquire(db, submission.id, mario).acquired is True
    assert lock_service.guard_review_save(db, submission.id, mario, 0).review_version == 1


def test_the_routes(db, clock, submission, mario, luca):
    opened = acquire_submission_lock(submission_id=submission.id, db=db, user=mario)
    assert opened.acquired is True

    refused = acquire_submission_lock(submission_id=submission.id, db=db, user=luca)
    assert (refused.acquired, refused.lock.user_id) == (False, mario.id)

    taken = take_over_submission_lock(submission_id=submission.id, db=db, user=luca)
    assert (taken.acquired, taken.lock.user_id) == (True, luca.id)

    assert release_submission_lock(submission_id=submission.id, db=db, user=luca) is None
    db.refresh(submission)
    assert submission.locked_by is None


# ---------------------------------------------------------------------------
# display_name
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "first, last, full, expected",
    [
        ("Mario", "Rossi", None, "Mario Rossi"),
        ("  Mario ", None, "ignored", "Mario"),
        (None, "Rossi", None, "Rossi"),
        (None, None, "Full Name", "Full Name"),
        ("", "  ", "Full Name", "Full Name"),
        (None, None, None, "Unnamed user"),
        (None, None, "  ", "Unnamed user"),
    ],
)
def test_display_name(first, last, full, expected):
    user = User(
        email="never-shown@example.com",
        first_name=first,
        last_name=last,
        full_name=full,
    )
    assert lock_service.display_name(user) == expected
