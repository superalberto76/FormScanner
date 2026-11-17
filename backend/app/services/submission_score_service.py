# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The score of a submission, for the review page.

Loads the variant's scorable objects and the submission's QUESTION and MATRIX
results — two queries, however many sheets and results — and hands them to
``scoring_rules.submission_score``, the one place the rules and the choice of
objects live. Read-only, nothing persisted: computed on the fly at every read
of the submission, so after every review action too.
``Submission.total_score`` is the close's business.

Only an EXTRACTED submission has a score: in review (SCANNED) or closed
(REVIEWED, FINALIZED). In any other status there is none, even with a variant
already assigned (UNASSIGNED with a variant, MATCHED not yet extracted): a
"0 / N (0%)" before extraction would read as a real grade.

A broken invariant in the rules (``ValueError``: an option id the question
does not have, an index outside the grid, a placeholder neither flagged nor
pending) is logged and gives NO score instead of failing the read: the FE has
no error boundary, and the review of the submission must stay usable.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.enums import CLOSED_SUBMISSION_STATUSES, SubmissionStatus
from app.models.submission import Submission
from app.repositories import page_object_repo, submission_object_result_repo
from app.schemas.submission import SubmissionScoreRead
from app.services import scoring_rules

logger = logging.getLogger(__name__)

# The statuses that have a score (module docstring).
SCORED_STATUSES = frozenset({SubmissionStatus.SCANNED, *CLOSED_SUBMISSION_STATUSES})


def compute(db: Session, submission: Submission) -> SubmissionScoreRead | None:
    """The score triple of *submission*, ``None`` outside
    :data:`SCORED_STATUSES` or with no variant assigned, and on a
    broken invariant (module docstring)."""
    variant_id = submission.matched_form_variant_id
    if submission.status not in SCORED_STATUSES or variant_id is None:
        return None

    page_objects = page_object_repo.list_scorable_for_variant(db, variant_id)
    results = submission_object_result_repo.list_scorable_for_submission(
        db, submission.id
    )
    try:
        score = scoring_rules.submission_score(page_objects, results)
    except ValueError:
        logger.exception("Cannot score submission %s", submission.id)
        return None
    return SubmissionScoreRead(
        raw_score=score.raw_score,
        max_possible_score=score.max_possible_score,
        percentage=score.percentage,
    )
