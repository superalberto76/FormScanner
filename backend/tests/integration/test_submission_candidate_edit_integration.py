# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for the manual candidate edit.

``PATCH /submissions/{id}/candidate`` is a review save, so what this suite pins
down is mostly that it behaves like one:

  * the happy path — value stored, ``review_version`` bumped, the lock now the
    caller's, ``submission.candidate_edited`` recorded with old and new value;
  * normalisation — trimmed, blank → null, over 500 characters refused (a
    ``ValidationError`` on the body, which FastAPI answers with 422);
  * the review-save guard, unchanged — ``LOCK_LOST``, ``SUBMISSION_CHANGED``,
    ``NOT_REVIEWABLE``, each leaving the value untouched;
  * a no-op edit is still a save.

Skipped without TEST_DATABASE_URL, like the rest of tests/integration.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from app.api.v1.endpoints.submissions import edit_submission_candidate
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
from app.schemas.submission import SubmissionCandidateUpdate
from app.services import audit_service, submission_lock_service as lock_service
from app.services.submission_lock_service import (
    LOCK_LOST,
    NOT_REVIEWABLE,
    SUBMISSION_CHANGED,
)

T0 = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(lock_service, "_now", lambda: T0)
    return T0


def _make_user(db, first: str, last: str) -> User:
    u = User(
        email=f"cand-{uuid.uuid4().hex}@example.com",
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
def submission(db, mario, luca) -> Submission:
    project = FormProject(
        owner_id=mario.id,
        type=FormProjectType.EXAM,
        name="Candidate project",
        status=FormProjectStatus.ACTIVE,
    )
    db.add(project)
    db.flush()
    for user, role in ((mario, ProjectRole.OWNER), (luca, ProjectRole.COLLABORATOR)):
        db.add(FormProjectMember(project_id=project.id, user_id=user.id, role=role))
    batch = SubmissionBatch(
        form_project_id=project.id, name="Batch", created_by_user_id=mario.id
    )
    db.add(batch)
    db.flush()
    s = Submission(
        submission_batch_id=batch.id,
        status=SubmissionStatus.SCANNED,
        respondent_identifier="0045871",
    )
    db.add(s)
    db.flush()
    return s


def _edit(db, submission, user, value, version=0):
    return edit_submission_candidate(
        submission_id=submission.id,
        data=SubmissionCandidateUpdate(respondent_identifier=value, review_version=version),
        db=db,
        user=user,
    )


def _events(db, submission) -> list[AuditLog]:
    return list(
        db.scalars(
            select(AuditLog).where(
                AuditLog.action == audit_service.SUBMISSION_CANDIDATE_EDITED,
                AuditLog.entity_id == str(submission.id),
            )
        ).all()
    )


def _conflict(excinfo, code: str) -> dict:
    exc = excinfo.value
    assert exc.status_code == 409
    assert exc.detail["code"] == code
    return exc.detail


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_an_edit_stores_the_value_bumps_the_version_and_is_audited(
    db, clock, submission, mario
):
    state = _edit(db, submission, mario, "0045817")

    assert state.respondent_identifier == "0045817"
    assert state.review_version == 1
    assert state.lock is not None
    assert state.lock.user_id == mario.id
    assert state.lock.display_name == "Mario Rossi"
    db.expire_all()
    stored = db.get(Submission, submission.id)
    assert stored.respondent_identifier == "0045817"
    assert stored.review_version == 1
    assert stored.locked_by == mario.id
    (event,) = _events(db, submission)
    assert event.actor_user_id == mario.id
    assert event.metadata_json == {"old_value": "0045871", "new_value": "0045817"}


def test_the_value_is_trimmed(db, clock, submission, mario):
    assert _edit(db, submission, mario, "  0099  ").respondent_identifier == "0099"


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_a_blank_value_clears_the_candidate(db, clock, submission, mario, blank):
    state = _edit(db, submission, mario, blank)

    assert state.respondent_identifier is None
    db.expire_all()
    assert db.get(Submission, submission.id).respondent_identifier is None
    assert _events(db, submission)[0].metadata_json["new_value"] is None


def test_over_500_characters_is_refused_by_the_body_schema():
    """FastAPI answers a body ValidationError with 422."""
    with pytest.raises(ValidationError):
        SubmissionCandidateUpdate(respondent_identifier="x" * 501, review_version=0)
    # 500 is the column size, and fits; surrounding blanks do not count.
    ok = SubmissionCandidateUpdate(respondent_identifier=" " + "x" * 500 + " ", review_version=0)
    assert ok.respondent_identifier == "x" * 500


def test_a_no_op_edit_is_still_a_save(db, clock, submission, mario):
    state = _edit(db, submission, mario, "0045871")

    assert state.review_version == 1
    assert len(_events(db, submission)) == 1


def test_consecutive_edits_declare_the_returned_version(db, clock, submission, mario):
    first = _edit(db, submission, mario, "A1")
    second = _edit(db, submission, mario, "A2", version=first.review_version)

    assert second.review_version == 2


# ---------------------------------------------------------------------------
# The review-save guard
# ---------------------------------------------------------------------------


def test_an_edit_under_a_colleagues_lock_is_lock_lost(db, clock, submission, mario, luca):
    submission.locked_by = luca.id
    submission.locked_at = T0 - timedelta(minutes=2)
    db.flush()

    with pytest.raises(HTTPException) as excinfo:
        _edit(db, submission, mario, "0045817")

    detail = _conflict(excinfo, LOCK_LOST)
    assert detail["locked_by"]["display_name"] == "Luca Bianchi"
    # Refused before anything was written.
    assert submission.respondent_identifier == "0045871"
    assert submission.review_version == 0
    assert _events(db, submission) == []


def test_an_edit_from_a_stale_screen_is_submission_changed(db, clock, submission, mario):
    submission.review_version = 4
    db.flush()

    with pytest.raises(HTTPException) as excinfo:
        _edit(db, submission, mario, "0045817", version=3)

    assert _conflict(excinfo, SUBMISSION_CHANGED)["current_version"] == 4
    assert _events(db, submission) == []


@pytest.mark.parametrize(
    "status", [SubmissionStatus.MATCHED, SubmissionStatus.REVIEWED]
)
def test_an_edit_outside_scanned_is_not_reviewable(db, clock, submission, mario, status):
    submission.status = status
    db.flush()

    with pytest.raises(HTTPException) as excinfo:
        _edit(db, submission, mario, "0045817")

    _conflict(excinfo, NOT_REVIEWABLE)
    assert _events(db, submission) == []


def test_a_non_member_gets_the_usual_403(db, clock, submission):
    outsider = _make_user(db, "Out", "Sider")

    with pytest.raises(HTTPException) as excinfo:
        _edit(db, submission, outsider, "0045817")

    assert excinfo.value.status_code == 403
