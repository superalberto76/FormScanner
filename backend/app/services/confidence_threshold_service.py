# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The confidence threshold and what it decides.

Two questions, and nothing else:

  * :func:`effective_threshold` — WHICH threshold applies to a batch. A batch
    may override the system-wide ``settings.default_confidence_threshold``; a
    NULL override is not a missing value but a deliberate "follow the default",
    so it resolves at read time and a later change to the default reaches the
    batch.
  * :func:`recompute_review_required_for_batch` — what changing that threshold
    does to results already stored; :func:`recompute_review_required_for_submission`
    the same for one reopened submission, which a change made while it was
    closed did not reach.

Why ``review_required`` is a persisted column and not a computed expression
--------------------------------------------------------------------------
It could be derived on the fly from ``confidence_score``, ``validity_forces_review``
and the batch's threshold. It must not be. Once a human has reviewed a result,
the flag it carried AT REVIEW TIME is part of the record of that review, and a
computed value would rewrite that history every time somebody moved the
threshold. So the flag is written down, and this module rewrites it only where
rewriting it is still honest: on results NO human has reviewed yet.

That "not yet reviewed" test is ``review_outcome IS NULL``: the result
carries no current human ruling. Not the existence of a
``submission_reviews`` row — that log is append-only and records UNDO too — and
not ``SubmissionObjectResult.reviewed``, which nothing writes (see
``submission_object_result_repo``). An UNDO puts a result back under this
module's authority: from then on it is recomputed like one never reviewed.

The two axes, and why they are OR-ed
------------------------------------
A result needs a human when EITHER holds:

  * its aggregated confidence is below the effective threshold — the optical
    reading is not sure enough to be trusted;
  * ``validity_forces_review`` — the reading is not VALID, or its object type
    always needs a human whatever the confidence (FREE_TEXT_AREA), or the
    reading cannot be trusted whatever its confidence (an undecoded CODE; a
    respondent-ID grid with an ambiguous or empty column, or no grid at all).

A third fact forces review on its own, like ``validity_forces_review``:
``sheet_not_aligned`` — the page the reading was taken on could not be
aligned to its template, so the reading may be shifted whatever its
confidence. It survives every recomputation for the same reason.

They are independent: a reading can be perfectly confident and invalid, or
valid and barely legible. Neither implies the other, so neither can
subsume the other and the combination is an OR.

A ``confidence_score`` of NULL never contributes. NULL means "no confidence was
ever produced for this reading" — the result predates extraction, or its type
has no such axis — and an unknown value must not be read as a bad one. Such a
result is left to ``validity_forces_review`` alone to decide.

This module recomputes ``review_required`` and NOTHING else:
``confidence_score``, ``validity_forces_review`` and ``sheet_not_aligned`` are
facts of the optical reading, written once by the extractor and never touched
here. It also never flushes or commits — the caller owns the transaction,
which is what lets a threshold change and its recomputation land atomically.
"""
from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.submission_batch import SubmissionBatch
from app.models.submission_object_result import SubmissionObjectResult
from app.repositories import submission_batch_repo, submission_object_result_repo


def effective_threshold(batch: SubmissionBatch) -> float:
    """The confidence threshold in force for *batch*.

    Its own override when it has one, the system default otherwise. Always
    resolve a threshold through here: reading ``batch.confidence_threshold``
    raw treats "follow the default" as "no threshold at all".
    """
    return (
        batch.confidence_threshold
        if batch.confidence_threshold is not None
        else settings.default_confidence_threshold
    )


def compute_review_required(
    result: SubmissionObjectResult, threshold: float
) -> bool:
    """Whether *result* needs a human, under *threshold*.

    Strictly BELOW the threshold triggers review: a confidence exactly equal to
    it is, by definition, sure enough. A NULL ``confidence_score`` contributes
    nothing either way — see the module docstring. ``sheet_not_aligned``
    forces review like ``validity_forces_review``, whatever the confidence and
    the threshold.
    """
    below_threshold = (
        result.confidence_score is not None
        and result.confidence_score < threshold
    )
    return (
        below_threshold
        or result.validity_forces_review
        or result.sheet_not_aligned
    )


def recompute_review_required_for_batch(
    db: Session, batch_id: uuid.UUID
) -> int:
    """Re-derive ``review_required`` across *batch_id*, sparing reviewed results
    and closed submissions.

    Walks every result of the batch with no current ruling
    (``review_outcome IS NULL``, see above) and rewrites its ``review_required`` from the two axes
    and the batch's now-current effective threshold. Results a human HAS
    reviewed are not read, not compared and not written: their flag is frozen at
    what it was when the review happened — until an UNDO, after which it moves
    with the threshold again. Nor are the results of a CLOSED submission:
    what was left unresolved at closing stays exactly that.

    Returns how many rows actually CHANGED value — not how many were examined.
    That is the number worth recording: "the threshold moved and 12 results
    changed side" tells an operator something, whereas the count of results in
    the batch tells them nothing they could not already see.

    Does NOT flush or commit. The caller owns the transaction, so moving a
    threshold and recomputing what it implies is one atomic act.
    """
    batch = submission_batch_repo.get(db, batch_id)
    if batch is None:
        # Defensive only: every caller resolves (and authorises) the batch
        # first. Nothing to recompute for a batch that is not there.
        return 0

    return _recompute(
        submission_object_result_repo.list_unreviewed_for_batch(db, batch_id),
        effective_threshold(batch),
    )


def recompute_review_required_for_submission(
    db: Session, batch: SubmissionBatch, submission_id: uuid.UUID
) -> int:
    """Re-derive ``review_required`` on the unresolved results of ONE
    submission of *batch*, with the batch's current effective threshold — the
    same rule as :func:`recompute_review_required_for_batch`, on a narrower
    set.

    For reopening a submission: while it was closed, a threshold change did
    not reach its results, and once open again it must follow the threshold
    in force like any other open submission. Reviewed results are spared
    exactly as above. Returns how many rows changed value. Does NOT flush or
    commit.
    """
    return _recompute(
        submission_object_result_repo.list_unreviewed_for_submission(
            db, submission_id
        ),
        effective_threshold(batch),
    )


def _recompute(results: list[SubmissionObjectResult], threshold: float) -> int:
    changed = 0
    for result in results:
        new_value = compute_review_required(result, threshold)
        if result.review_required != new_value:
            result.review_required = new_value
            changed += 1
    return changed
