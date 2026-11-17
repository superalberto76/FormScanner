# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The candidate set by a respondent-ID reading.

A respondent ID read with no need for review becomes the submission's
candidate. WHICH reading qualifies is ``review_rules.candidate_from_reading``;
this module is WHEN and on WHICH submission it is applied, for its two
callers:

  * the extraction run, at the end of each submission it walks
    (``extraction_orchestrator_service``);
  * a change of the batch's confidence threshold, after the recomputation
    (``submission_batch_service.update_confidence_threshold``): a respondent
    ID that a lower threshold lets out of review sets the candidate as if it
    had been read that way.

The rule, on top of the reading's:

  * the submission is open — MATCHED or SCANNED. A closed one (REVIEWED /
    FINALIZED) is never touched;
  * it has no candidate. One already there — a RID at upload, a CONFIRM or a
    SUBSTITUTE, a manual edit — is NEVER overwritten, the same precedence as
    ``submission_identification_service.lift_respondent_identifier``;
  * no review save has happened on it (``review_version`` 0): once a human
    has worked on the submission, an empty candidate is their state — a
    candidate cleared by hand stays cleared;
  * only the FIRST respondent-ID reading in physical page order speaks: with
    duplicate sheets, if that one does not qualify — in review, incomplete —
    there is no candidate, even if a later sheet reads clean. The respondent
    ID decides whose answers these are. Review follows the same rule: a
    CONFIRM or SUBSTITUTE sets the candidate only on the first identity
    reading (``review_rules.first_identifier_reading``), so a linked copy of
    a shared respondent ID on a later page never overrides it.

The candidate is never cleared here either: a threshold raised over a
respondent ID already used as the candidate sends it to review and leaves the
candidate as it is; CONFIRM or SUBSTITUTE update it.

Like the extraction run and the threshold change it belongs to, it neither
bumps ``review_version`` nor looks at the review lock: it only fills a
candidate that was missing. But it decides on the submission as it is NOW in
the database, row-locked, never on a copy loaded earlier: an extraction run
takes minutes, and an operator may confirm, edit or close the submission in
the meantime. In the run the lock is taken only on a submission that is about
to receive a candidate. Never commits: the caller owns the transaction.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy.orm import Session

from app.models.enums import SubmissionStatus
from app.models.submission import Submission
from app.models.submission_object_result import SubmissionObjectResult
from app.repositories import submission_batch_repo, submission_object_result_repo
from app.services import review_rules

_OPEN_STATUSES = (SubmissionStatus.MATCHED, SubmissionStatus.SCANNED)

# What the rule reads on the submission, re-read from the database before it
# is applied.
_GUARDED_ATTRIBUTES = ["status", "respondent_identifier", "review_version"]


def may_receive_candidate(submission: Submission) -> bool:
    """Whether *submission*, as held, may still receive a candidate: open, with
    no candidate, never saved on in review (module docstring)."""
    return (
        submission.status in _OPEN_STATUSES
        and not submission.respondent_identifier
        and submission.review_version == 0
    )


def _candidate_from_first_reading(
    readings: Sequence[SubmissionObjectResult],
) -> str | None:
    """The candidate the first of *readings* (the submission's own, in page
    order) spells, if it qualifies."""
    if not readings:
        return None
    first = readings[0]
    return review_rules.candidate_from_reading(first, first.page_object)


def _set_from_first_reading(
    submission: Submission, readings: Sequence[SubmissionObjectResult]
) -> bool:
    """Set the candidate from the first of *readings*, if it qualifies.
    Returns whether it was set."""
    value = _candidate_from_first_reading(readings)
    if value is None:
        return False
    submission.respondent_identifier = value
    return True


def apply_candidate_from_extraction(db: Session, submission: Submission) -> bool:
    """Give *submission* the candidate its respondent-ID reading spells, if
    the rule allows it (module docstring). Returns whether the candidate was
    set.

    A submission that, as held, cannot receive one is left alone without a
    statement: the copy can only be stale towards a candidate set or a save
    made since, never the other way. Otherwise the staged work is flushed (the
    query must see the readings the run just staged) and its respondent-ID
    readings are read. Reads EVERY respondent-ID reading of the submission,
    not only those of the current run, so a submission whose earlier
    extraction set no candidate gets it on the next run.

    Only when the first reading does spell a candidate is the submission
    re-read under a row lock and the rule decided on that: a lock taken
    before would be held until the run commits even when nothing is written —
    on every submission of an anonymous questionnaire, and on one whose
    respondent ID waits in review, the very one an operator is about to open.
    Reading first is safe: a review save that changed the reading has also
    raised ``review_version``, which is checked under the lock.
    """
    if not may_receive_candidate(submission):
        return False
    db.flush()
    readings = submission_object_result_repo.list_respondent_identifier_readings(
        db, [submission.id]
    )
    value = _candidate_from_first_reading(readings)
    if value is None:
        return False
    db.refresh(submission, attribute_names=_GUARDED_ATTRIBUTES, with_for_update=True)
    if not may_receive_candidate(submission):
        return False
    submission.respondent_identifier = value
    return True


def apply_candidates_for_batch(db: Session, batch_id: uuid.UUID) -> int:
    """:func:`apply_candidate_from_extraction` over every submission of
    *batch_id* that may still receive a candidate, in two queries however many
    there are; the submissions are selected row-locked. Returns how many
    candidates were set. Does not flush: the caller flushes the recomputation
    first, so the readings are read as they now stand."""
    submissions = submission_batch_repo.list_open_submissions_without_candidate(
        db, batch_id
    )
    readings_by_submission: dict[uuid.UUID, list[SubmissionObjectResult]] = {}
    for result in submission_object_result_repo.list_respondent_identifier_readings(
        db, [submission.id for submission in submissions]
    ):
        readings_by_submission.setdefault(
            result.submission_page.submission_id, []
        ).append(result)

    candidates_set = 0
    for submission in submissions:
        if may_receive_candidate(submission) and _set_from_first_reading(
            submission, readings_by_submission.get(submission.id, [])
        ):
            candidates_set += 1
    return candidates_set
