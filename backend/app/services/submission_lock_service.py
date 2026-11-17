# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Review concurrency for one submission: soft lock + review_version.

Several operators review the same batch at once, so two complementary
mechanisms keep them from overwriting each other, both per submission:

  * the SOFT LOCK — who is working on it. ``locked_by`` / ``locked_at``,
    acquired on open, renewed on open and on every review save, expired after
    ``LOCK_TIMEOUT`` of inactivity, released on exit (best-effort: a closed tab
    sends nothing, which is what the expiry is for). Anyone may take it over
    explicitly — that is also the whole of "admin unlock", which does not exist.
    It is BINDING on saves: whoever does not hold it cannot save.
  * ``review_version`` — nobody saves from a stale state. Every review save
    bumps it and declares the version it was built on; a mismatch is refused.
    The lock alone cannot give this: Mario, idle past the timeout while Luca
    reviewed and left, finds the lock free again — but his screen is stale.

An expired lock is IGNORED, never cleaned up: no job, no scheduler. Its stale
values stay in the row until the next acquire overwrites them, so "is it
locked?" is always :func:`is_lock_active`, never ``locked_by IS NOT NULL``.

Every write of the lock or of the version reads the row ``SELECT … FOR UPDATE``
(``submission_batch_repo.get_submission_for_update``): the check and the write
must see the same row, or two simultaneous opens would both acquire.

Authorisation is the submission's usual one — project membership, through
``submission_batch_service.load_submission_authorized`` — with NO gate on the
project being ACTIVE: review must keep working once collection has closed, on
CLOSED and ARCHIVED projects alike.

Structured 409s. This is the FIRST place in the API whose error ``detail`` is
an object rather than a sentence: ``{"code", "message", ...extras}``. The FE
must tell three refusals apart and react differently to each — "Luca took
over" (show who, offer to take back), "the submission changed under you"
(reload), "not reviewable" (leave) — and parsing English text for that would
break the first time a message is reworded. ``code`` is the contract;
``message`` is for humans.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterable

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.enums import SubmissionStatus
from app.models.submission import Submission
from app.models.user import User
from app.repositories import user_repo
from app.schemas.submission import SubmissionLockInfo, SubmissionLockState
from app.services import audit_service, submission_batch_service

# Inactivity after which a lock stops counting. Fixed by design (not
# configurable): long enough to read a hard sheet, short enough that a
# forgotten tab does not block a colleague for the afternoon.
LOCK_TIMEOUT = timedelta(minutes=15)

# The statuses a submission can be locked — and so reviewed — in. SCANNED
# only: a REVIEWED one is reopened first (``submission_close_service``), which
# brings it back to SCANNED with the lock already the reopener's, and a
# MATCHED submission, even with partial results, must be re-extracted before
# anyone reviews it. Closing is a review save, so it is allowed from these
# statuses only too (``submission_close_service``).
LOCKABLE_STATUSES = frozenset({SubmissionStatus.SCANNED})

# Machine-readable codes of the structured 409s (see the module docstring).
NOT_REVIEWABLE = "NOT_REVIEWABLE"
LOCK_LOST = "LOCK_LOST"
SUBMISSION_CHANGED = "SUBMISSION_CHANGED"
# Not a submission code: an extraction run holds the submission's batch
# (``batch_operation_lock``). Here so every structured 409 code is in one
# place.
EXTRACTION_RUNNING = "EXTRACTION_RUNNING"
# Not a submission code either: the project's published variants are not
# configured for scanning, so batch creation and uploads are refused.
NOT_SCAN_READY = "NOT_SCAN_READY"
# The recovery actions of a MATCH_FAILED submission: assigning a variant or
# marking it unresolvable (also from UNASSIGNED) from a status that does not
# allow it (with ``status``), and assigning a variant that is not PUBLISHED.
INVALID_STATUS = "INVALID_STATUS"
VARIANT_NOT_PUBLISHED = "VARIANT_NOT_PUBLISHED"
# Not a submission code: the batch cannot be exported yet
# (``submission_batch_service.require_ready_for_export``), with the counts of
# what holds it back.
NOT_EXPORT_READY = "NOT_EXPORT_READY"

UNNAMED_USER = "Unnamed user"


def _now() -> datetime:
    """The current instant, timezone-aware UTC.

    The ONE clock of this module — every lock comparison and every
    ``locked_at`` write goes through it, so tests move time by patching it.
    """
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Reading a lock
# ---------------------------------------------------------------------------


def display_name(user: User) -> str:
    """How a lock holder is named to other operators.

    ``first_name last_name`` when either is set; else the single-name
    ``full_name``, which older users may still carry; else a placeholder. Never
    the email: the project member roster deliberately excludes it.
    """
    parts = [p.strip() for p in (user.first_name, user.last_name) if p and p.strip()]
    if parts:
        return " ".join(parts)
    if user.full_name and user.full_name.strip():
        return user.full_name.strip()
    return UNNAMED_USER


def is_lock_active(submission: Submission, now: datetime | None = None) -> bool:
    """Whether *submission* carries a lock that still counts.

    Held (``locked_by`` set) AND last active strictly less than
    ``LOCK_TIMEOUT`` ago — at exactly the timeout it has expired.
    """
    if submission.locked_by is None or submission.locked_at is None:
        return False
    return (now or _now()) - submission.locked_at < LOCK_TIMEOUT


def lock_info(submission: Submission, holder: User) -> SubmissionLockInfo:
    """The API view of *submission*'s lock, held by *holder*. The caller has
    already established that the lock is active."""
    return SubmissionLockInfo(
        user_id=holder.id,
        display_name=display_name(holder),
        locked_at=_utc(submission.locked_at),
    )


def _utc(moment: datetime) -> datetime:
    # A timestamptz comes back in the DB session's time zone; everything this
    # module emits (API, audit metadata) is UTC, whatever that zone is.
    return moment.astimezone(timezone.utc)


def active_locks(
    db: Session, submissions: Iterable[Submission]
) -> dict[uuid.UUID, SubmissionLockInfo]:
    """{submission_id: its active lock} for a listing, absent when none.

    For the batch queue: every lock judged at ONE instant, and every holder
    loaded by ONE query (none at all when nothing is locked), so the cost does
    not grow with the number of submissions.
    """
    now = _now()
    locked = [s for s in submissions if is_lock_active(s, now)]
    holders = user_repo.list_by_ids(db, {s.locked_by for s in locked})
    return {s.id: lock_info(s, holders[s.locked_by]) for s in locked}


def _held_by_other(submission: Submission, user: User, now: datetime) -> bool:
    return is_lock_active(submission, now) and submission.locked_by != user.id


def _holder(db: Session, submission: Submission) -> User:
    # Identity-map backed: no query when the holder is the caller.
    return user_repo.get_by_id(db, submission.locked_by)


# ---------------------------------------------------------------------------
# Structured 409s
# ---------------------------------------------------------------------------


def structured_conflict(code: str, message: str, **extras) -> HTTPException:
    """A 409 whose ``detail`` is ``{"code", "message", ...extras}`` (see the
    module docstring). Public so every review write of the submission —
    "Review anyway" too — refuses in the same shape the FE already parses."""
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": code, "message": message, **extras},
    )


def _require_lockable(submission: Submission) -> None:
    if submission.status not in LOCKABLE_STATUSES:
        raise structured_conflict(
            NOT_REVIEWABLE,
            f"This submission cannot be reviewed in status {submission.status.value}.",
            status=submission.status.value,
        )


def _lock_lost(db: Session, submission: Submission) -> HTTPException:
    holder = lock_info(submission, _holder(db, submission))
    return structured_conflict(
        LOCK_LOST,
        f"{holder.display_name} is now reviewing this submission.",
        locked_by={
            "user_id": str(holder.user_id),
            "display_name": holder.display_name,
            "locked_at": holder.locked_at.isoformat(),
        },
    )


# ---------------------------------------------------------------------------
# Lock lifecycle (each owns its transaction)
# ---------------------------------------------------------------------------


def _load_for_update(
    db: Session, submission_id: uuid.UUID, user: User
) -> Submission:
    submission, _project = submission_batch_service.load_submission_authorized(
        db, submission_id, user, for_update=True
    )
    return submission


def take(submission: Submission, user: User, now: datetime | None = None) -> None:
    """Write *user*'s lock on *submission*, active from *now* (this module's
    clock by default). The ONE write of a lock: acquire, take-over, every
    review save and reopening a submission (which starts from REVIEWED and so
    cannot go through :func:`guard_review_save`). No check, no flush:
    the caller has read the row ``FOR UPDATE`` and decided."""
    # Acquire and renew are the same write: locked_at is the LAST activity.
    submission.locked_by = user.id
    submission.locked_at = now or _now()


def _state(
    db: Session, submission: Submission, acquired: bool, now: datetime
) -> SubmissionLockState:
    # Judged at the same instant as the decision, so the reported lock is
    # exactly the one the caller just acquired or was refused by.
    lock = (
        lock_info(submission, _holder(db, submission))
        if is_lock_active(submission, now)
        else None
    )
    return SubmissionLockState(
        acquired=acquired, lock=lock, review_version=submission.review_version
    )


def acquire(
    db: Session, submission_id: uuid.UUID, user: User
) -> SubmissionLockState:
    """Lock a submission for *user* on opening it, unless a colleague holds it.

    Free, expired, or already the caller's: the caller gets (or renews) it.
    Actively held by someone else: NOTHING changes and ``acquired`` is false,
    with the holder reported — the FE then offers Take over. 409
    ``NOT_REVIEWABLE`` outside ``LOCKABLE_STATUSES``. Not audited: noise.
    """
    submission = _load_for_update(db, submission_id, user)
    _require_lockable(submission)
    now = _now()
    acquired = not _held_by_other(submission, user, now)
    if acquired:
        take(submission, user, now)
    # Committed even when nothing changed: it is what releases the row lock.
    db.commit()
    return _state(db, submission, acquired, now)


def take_over(
    db: Session, submission_id: uuid.UUID, user: User
) -> SubmissionLockState:
    """Lock a submission for *user* whoever holds it — the explicit override.

    Always acquires (409 ``NOT_REVIEWABLE`` aside). Audited as
    ``submission.lock_taken_over`` only when an ACTIVE lock of another user
    was actually taken; on a free or expired lock it is a plain acquire.
    """
    submission = _load_for_update(db, submission_id, user)
    _require_lockable(submission)
    now = _now()
    if _held_by_other(submission, user, now):
        audit_service.log_event(
            db,
            actor_user_id=user.id,
            action=audit_service.SUBMISSION_LOCK_TAKEN_OVER,
            entity_type="Submission",
            entity_id=submission.id,
            metadata={
                "previous_holder_id": str(submission.locked_by),
                "previous_locked_at": _utc(submission.locked_at).isoformat(),
            },
        )
    take(submission, user, now)
    db.commit()
    return _state(db, submission, True, now)


def release(db: Session, submission_id: uuid.UUID, user: User) -> None:
    """Let go of the caller's lock on leaving the submission.

    Clears it only if the ACTIVE lock is the caller's; anything else — someone
    else's lock, an expired one, none — is left alone, with no error. Exit is
    best-effort (the request may arrive late, twice, or after a take-over), so
    it must be idempotent and must never undo a colleague's lock. Allowed in
    any status: leaving is always possible.
    """
    submission = _load_for_update(db, submission_id, user)
    if is_lock_active(submission, _now()) and submission.locked_by == user.id:
        submission.locked_by = None
        submission.locked_at = None
    db.commit()


# ---------------------------------------------------------------------------
# The guard every review write goes through
# ---------------------------------------------------------------------------


def guard_review_save(
    db: Session, submission_id: uuid.UUID, user: User, declared_version: int
) -> Submission:
    """Admit one review save on a submission, or refuse it with a structured
    409.

    Called by every review write (the review actions, the manual candidate
    edit) BEFORE it writes. Checks, in this order:

      1. not in ``LOCKABLE_STATUSES``  → ``NOT_REVIEWABLE``;
      2. actively locked by someone else → ``LOCK_LOST``, naming the holder.
         Before the version check on purpose: when both hold, "Luca took over"
         is the message that explains the situation — the version moved
         because Luca saved;
      3. *declared_version* is not the current one → ``SUBMISSION_CHANGED``,
         with ``current_version``.

    Admitted: the caller acquires the lock if free or expired and renews it if
    its own (a save is activity), ``review_version`` is bumped, and the
    submission is returned for the caller to write against.

    Does NOT commit. The review action owns the transaction, so its review rows
    and the version bump land — or roll back — together; the row stays locked
    FOR UPDATE until then.
    """
    submission = _load_for_update(db, submission_id, user)
    _require_lockable(submission)
    now = _now()
    if _held_by_other(submission, user, now):
        raise _lock_lost(db, submission)
    if declared_version != submission.review_version:
        raise structured_conflict(
            SUBMISSION_CHANGED,
            "This submission has changed since you opened it. Reload it before saving.",
            current_version=submission.review_version,
        )
    take(submission, user, now)
    submission.review_version += 1
    db.flush()
    return submission
