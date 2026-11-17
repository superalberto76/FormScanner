# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""One extraction run at a time per batch.

"Run extraction" is ONE transaction of minutes
(``submission_batch_service.run_batch_extraction``), and it reads at its start
what it later relies on: which (sheet, object) pairs are already extracted, and
the batch's confidence threshold. Three writers could change those under it:

  * a second run on the same batch — both read the same objects, and the
    uniqueness of ``(submission_page_id, page_object_id)`` would fail the later
    one whole, every other submission of the batch included;
  * "Review anyway" — its placeholders would take pairs the run was about to
    read, with the same result;
  * a change of the threshold — it re-derives ``review_required`` on the
    results already stored, while the run would keep computing its own with
    the threshold it had loaded.

So, while a run is in progress on a batch, a second run, a "Review anyway" on
one of its submissions and a change of its threshold are REFUSED with the
structured 409 ``EXTRACTION_RUNNING``; nobody waits. The operator sees the
message and tries again once the run is over.

The lock
--------
A PostgreSQL advisory lock tied to the transaction, keyed on the batch: the run
takes it EXCLUSIVE, "Review anyway" and the threshold change take it SHARED, so
those two never hold each other back. It is released by the commit or the
rollback, also when the process dies: there is no "run in progress" state to
clean up and nothing in the schema. Always the *try* variant, so nobody waits
and no wait can deadlock with the submissions' row locks.

A run is therefore also refused during the milliseconds a "Review anyway" or a
threshold change holds the batch; telling the two apart would mean reading
``pg_locks``, so the run's message covers both.

A session that already holds the lock gets it again, whatever the mode: the
calls are safe to repeat inside one transaction.

Take the batch BEFORE locking any of its submissions' rows: a run holds the
rows of the submissions it has walked until it commits, so a caller holding back
on one of those would wait out the run instead of being refused ("Review
anyway" does it in that order).

Known limitation, accepted: the key's second half is a 32-bit hash, so two
batches whose ids hash alike would refuse each other's runs. The odds are
negligible, and the cost is a 409 to retry, never a wrong write.

Two threshold changes on the same batch both take it shared, so they do not
serialise on it; the batch row's UPDATE orders them, and that is not this
lock's job.

Never commits: the caller owns the transaction, and the lock lives exactly as
long as it does.
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.services import submission_lock_service

# The first half of the two-int advisory key: the batch-operations namespace,
# so another future use of advisory locks picks a different number and never
# collides with these. The second half is ``hashtext`` of the batch id.
_NAMESPACE = 203

RUN_IN_PROGRESS_MESSAGE = (
    "An extraction run or another change is in progress on this batch. "
    "Try again in a moment."
)
CHANGE_REFUSED_MESSAGE = (
    "An extraction run is in progress on this batch. Try again when it finishes."
)


def _try(db: Session, batch_id: uuid.UUID, *, shared: bool) -> bool:
    lock = func.pg_try_advisory_xact_lock_shared if shared else func.pg_try_advisory_xact_lock
    return bool(db.scalar(select(lock(_NAMESPACE, func.hashtext(str(batch_id))))))


def acquire_for_run(db: Session, batch_id: uuid.UUID) -> None:
    """Take *batch_id* for an extraction run (exclusive), or raise the 409."""
    if not _try(db, batch_id, shared=False):
        raise submission_lock_service.structured_conflict(
            submission_lock_service.EXTRACTION_RUNNING, RUN_IN_PROGRESS_MESSAGE
        )


def acquire_shared(db: Session, batch_id: uuid.UUID) -> None:
    """Take *batch_id* for a change that must not overlap a run — "Review
    anyway", a threshold change — or raise the 409 if a run holds it."""
    if not _try(db, batch_id, shared=True):
        raise submission_lock_service.structured_conflict(
            submission_lock_service.EXTRACTION_RUNNING, CHANGE_REFUSED_MESSAGE
        )
