# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Review lock and review_version with two operators.

F04 goes through the real path (upload, matching, extraction); then the
owner (U1) and the project's COLLABORATOR (U2) review it as in the review
flow of the test kit, and the idle-reviewer example (Mario and Luca):

* U1 opens; U2 opening sees U1's lock and takes over (audited);
* U1's save and candidate edit are refused with LOCK_LOST naming U2, and
  write nothing; U1 takes back and saves;
* U2, back on a stale screen, is refused with SUBMISSION_CHANGED;
* release only by the holder; a lock idle for over 15 minutes leaves the
  queue and a colleague opens without taking over;
* Mario idle, Luca opens and saves, Mario is refused (LOCK_LOST, then
  SUBMISSION_CHANGED once Luca's lock has expired too), reloads, saves and
  gets the lock back; Luca's work stays.

After every step the queue, the review page and the project's batch list
agree on the pending count and the lock; the expected counts are declared
here, from F04's declared review. Time is the ``clock`` fixture.
Marked ``synthetic``: ``pytest -m synthetic tests/synthetic -s``.
"""
from __future__ import annotations

import shutil
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.audit_log import AuditLog
from app.models.enums import ReviewOutcome, SubmissionStatus
from app.models.user import User
from app.schemas.submission import ReviewActionType, SubmissionReviewActionRequest
from app.services import (
    audit_service,
    review_action_service,
    submission_batch_service,
    submission_lock_service,
    submission_review_service,
)
from app.services.submission_lock_service import LOCK_LOST, LOCK_TIMEOUT, SUBMISSION_CHANGED
from tests.synthetic.builders import COLLABORATOR_NAME, OWNER_NAME
from tests.synthetic.runner import queue_state, review_objects, upload_scenarios
from tests.synthetic.scenarios import SCENARIOS

pytestmark = [
    pytest.mark.synthetic,
    pytest.mark.skipif(
        shutil.which("tesseract") is None,
        reason="Tesseract not installed: the scenarios use real OCR",
    ),
]

F04 = SCENARIOS["F04"]
CANDIDATE = F04.expected.candidate  # "100004", read without review
# Just past the 15 minutes; the exact boundary is the integration tests'.
IDLE = LOCK_TIMEOUT + timedelta(seconds=1)


def _expect(db, batch, submission, user, *, pending: int, lock: str | None) -> None:
    state = queue_state(db, batch, submission, user)
    assert state.status is SubmissionStatus.SCANNED
    assert (state.pending, state.review_pending, state.batch_pending) == (pending,) * 3, state
    assert (state.lock, state.review_lock) == (lock, lock), state
    assert state.candidate == CANDIDATE


def _detail(db, submission, user):
    return submission_review_service.get_review_detail(db, submission.id, user)


def _save(db, submission, label: str, user: User, version: int, action=ReviewActionType.CONFIRM):
    result_id = review_objects(_detail(db, submission, user))[label].result_id
    return review_action_service.apply_action(
        db,
        submission.id,
        result_id,
        user,
        SubmissionReviewActionRequest(action=action, review_version=version),
    )


def _refused(db, call) -> dict:
    """A save expected to be refused with a 409; rolled back as ``get_db``
    would, so the assertions after it see exactly what the refusal wrote."""
    db.commit()
    with pytest.raises(HTTPException) as excinfo:
        call()
    db.rollback()
    assert excinfo.value.status_code == 409, excinfo.value.detail
    return excinfo.value.detail


def _takeovers(db, submission) -> list[AuditLog]:
    return list(
        db.scalars(
            select(AuditLog).where(
                AuditLog.action == audit_service.SUBMISSION_LOCK_TAKEN_OVER,
                AuditLog.entity_id == str(submission.id),
            )
        ).all()
    )


def _untouched(db, submission, user, label: str, version: int) -> None:
    detail = _detail(db, submission, user)
    assert detail.review_version == version
    assert not review_objects(detail)[label].resolved


def test_lock_and_version_with_two_operators(db, scan_storage, clock, coll_multi):
    u1, batch, subs = upload_scenarios(db, coll_multi, "Lock", [F04])
    u2 = db.get(User, coll_multi.collaborator_id)
    submission = subs["F04"]
    summary = submission_batch_service.run_batch_extraction(db, batch.id, u1)
    assert summary.errors == 0, summary.error_details
    declared = len(F04.expected.review)  # Q01, Q02, M-RIGHE, M-COLONNE, NOME, APERTA
    _expect(db, batch, submission, u1, pending=declared, lock=None)
    assert _detail(db, submission, u1).review_version == 0

    # U1 opens F04 — "🔒 You" in the queue.
    state = submission_lock_service.acquire(db, submission.id, u1)
    assert state.acquired and state.lock.display_name == OWNER_NAME
    _expect(db, batch, submission, u1, pending=declared, lock=OWNER_NAME)

    # U2 opens it — "In review by Coll Owner", read-only; nothing changes.
    state = submission_lock_service.acquire(db, submission.id, u2)
    assert not state.acquired
    assert state.lock.display_name == OWNER_NAME
    assert state.review_version == 0
    _expect(db, batch, submission, u2, pending=declared, lock=OWNER_NAME)

    # ... and takes over: audited, U1 named as the previous holder.
    state = submission_lock_service.take_over(db, submission.id, u2)
    assert state.acquired and state.lock.display_name == COLLABORATOR_NAME
    (event,) = _takeovers(db, submission)
    assert event.actor_user_id == u2.id
    assert event.metadata_json["previous_holder_id"] == str(u1.id)
    _expect(db, batch, submission, u1, pending=declared, lock=COLLABORATOR_NAME)

    # U1's save and candidate edit are refused naming U2; nothing written.
    refusal = _refused(db, lambda: _save(db, submission, "Q01", u1, 0))
    assert refusal["code"] == LOCK_LOST
    assert refusal["locked_by"]["display_name"] == COLLABORATOR_NAME
    refusal = _refused(
        db,
        lambda: submission_review_service.edit_candidate(db, submission.id, u1, "999999", 0),
    )
    assert refusal["code"] == LOCK_LOST
    assert refusal["locked_by"]["display_name"] == COLLABORATOR_NAME
    _untouched(db, submission, u1, "Q01", 0)
    _expect(db, batch, submission, u1, pending=declared, lock=COLLABORATOR_NAME)

    # Take back, then the save goes through: version 1.
    submission_lock_service.take_over(db, submission.id, u1)
    assert len(_takeovers(db, submission)) == 2
    detail = _save(db, submission, "Q01", u1, 0)
    assert detail.review_version == 1
    assert review_objects(detail)["Q01"].review_outcome is ReviewOutcome.CONFIRMED
    _expect(db, batch, submission, u1, pending=declared - 1, lock=OWNER_NAME)

    # U2 takes over again but saves from the screen it had (version 0).
    submission_lock_service.take_over(db, submission.id, u2)
    assert len(_takeovers(db, submission)) == 3
    refusal = _refused(db, lambda: _save(db, submission, "Q02", u2, 0))
    assert refusal["code"] == SUBMISSION_CHANGED
    assert refusal["current_version"] == 1
    _untouched(db, submission, u2, "Q02", 1)
    # Reloaded, the same save goes through: version 2.
    assert _save(db, submission, "Q02", u2, 1).review_version == 2
    _expect(db, batch, submission, u2, pending=declared - 2, lock=COLLABORATOR_NAME)

    # Leaving releases the lock — only the holder's leave counts.
    submission_lock_service.release(db, submission.id, u1)
    _expect(db, batch, submission, u1, pending=declared - 2, lock=COLLABORATOR_NAME)
    submission_lock_service.release(db, submission.id, u2)
    _expect(db, batch, submission, u1, pending=declared - 2, lock=None)

    # A lock idle for over 15 minutes leaves the queue, and U2 opens without
    # taking over (no audit).
    submission_lock_service.acquire(db, submission.id, u1)
    clock.advance(minutes=14)
    _expect(db, batch, submission, u2, pending=declared - 2, lock=OWNER_NAME)
    clock.advance(seconds=IDLE.total_seconds() - 14 * 60)
    _expect(db, batch, submission, u2, pending=declared - 2, lock=None)
    state = submission_lock_service.acquire(db, submission.id, u2)
    assert state.acquired and state.lock.display_name == COLLABORATOR_NAME
    assert len(_takeovers(db, submission)) == 3
    submission_lock_service.release(db, submission.id, u2)

    # Idle-reviewer example: Mario (U1) opens at version 2 and walks away.
    mario_version = _detail(db, submission, u1).review_version
    assert mario_version == 2
    submission_lock_service.acquire(db, submission.id, u1)
    clock.advance(seconds=IDLE.total_seconds())
    # Luca (U2) opens without taking over, reviews and saves.
    assert submission_lock_service.acquire(db, submission.id, u2).acquired
    luca = _save(db, submission, "M-RIGHE", u2, mario_version, ReviewActionType.FLAG)
    assert luca.review_version == 3
    # Mario's save is refused: Luca holds the lock ...
    refusal = _refused(db, lambda: _save(db, submission, "M-COLONNE", u1, mario_version))
    assert refusal["code"] == LOCK_LOST
    assert refusal["locked_by"]["display_name"] == COLLABORATOR_NAME
    # ... and, once Luca's lock has expired too, because his screen is stale.
    clock.advance(seconds=IDLE.total_seconds())
    refusal = _refused(db, lambda: _save(db, submission, "M-COLONNE", u1, mario_version))
    assert refusal["code"] == SUBMISSION_CHANGED
    assert refusal["current_version"] == 3
    _untouched(db, submission, u1, "M-COLONNE", 3)
    # Mario reloads and saves: he gets the lock back; Luca's FLAG is intact.
    reloaded = _detail(db, submission, u1).review_version
    detail = _save(db, submission, "M-COLONNE", u1, reloaded)
    assert detail.review_version == 4
    flagged = review_objects(detail)["M-RIGHE"]
    assert flagged.review_outcome is ReviewOutcome.FLAGGED
    assert flagged.reviewed_by.display_name == COLLABORATOR_NAME
    _expect(db, batch, submission, u1, pending=declared - 4, lock=OWNER_NAME)
    assert set(review_objects(detail)) == set(F04.expected.review)

    print(
        f"\n[synthetic] lock: {len(_takeovers(db, submission))} take-over, "
        f"final version {detail.review_version}, pending {declared} → {declared - 4}"
    )
