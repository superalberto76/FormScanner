# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The operator's actions on a reading, and "Accept all remaining".

Four actions on ONE ``SubmissionObjectResult``, each with an optional note
(:func:`apply_action`):

  * CONFIRM    — the reading is right as read;
  * SUBSTITUTE — the reading is wrong, and the operator enters the right value;
  * FLAG       — unresolvable from the sheet: no answer, with a reason of its
                 own (distinct from a SUBSTITUTE with nothing selected, which
                 says "the candidate did not answer");
  * UNDO       — back to "not reviewed".

Plus :func:`accept_remaining`: CONFIRM, in one save, every still-unresolved
reading of the submission that may be accepted unseen.

Current state and history
-------------------------
Every action writes BOTH, in one transaction: the current ruling on the result
(``review_outcome``, ``review_note``, ``reviewed_by``, ``reviewed_at`` and,
for SUBSTITUTE, ``reviewed_payload_json``), which the review page reads; and
one APPEND-ONLY ``SubmissionReview`` row, the history, which nothing ever
updates or deletes. UNDO clears the first and appends to the second. That log
IS the trail of review actions, so no ``audit_logs`` event is written per
action — only "Accept all remaining", one act touching many results, logs one.

CONFIRM, SUBSTITUTE and FLAG are allowed on a result already resolved: they
overwrite its current ruling, and the log keeps what it was. UNDO is allowed
ONLY on a resolved result (409 ``NOT_RESOLVED``): to postpone an unresolved
one, the operator simply moves on.

Which readings may be confirmed, and which bulk-accepted, is
``review_rules`` — the one place those rules live, also applied by the read.

Order of checks
---------------
  1. the result must belong to the submission — 404 otherwise;
  2. ``submission_lock_service.guard_review_save``: SCANNED, the lock, the
     version — its structured 409s; admitted, it renews the caller's lock
     and bumps ``review_version``;
  3. the action's own rules: CONFIRM of an incomplete reading → 409
     ``NOT_CONFIRMABLE`` with a ``reason``; SUBSTITUTE without a valid
     ``value`` → 422; a ``value`` with any other action → 422; UNDO of an
     unresolved result → 409 ``NOT_RESOLVED``.

A refusal at step 3 raises before the commit, so the version bump of step 2 is
rolled back with everything else.

The reviewed value
------------------
A SUBSTITUTE's ``value`` is validated against the reading's type and written in
the reviewed-payload shape of ``app.schemas.submission_result``, its validity
ALWAYS computed by the server with the extractors' own functions
(``question_extraction_service.compute_validity``,
``matrix_extraction_service.compute_group_validity``):

  * QUESTION ``{selected_option_ids}`` — options of THIS question, no
    duplicates, empty allowed (NO_ANSWER);
  * MATRIX ``{selected_cells: [[row, col], …]}`` — in range, no duplicates,
    empty allowed; grouped by row or by column like the detection payload;
  * candidate identifier (a respondent-ID grid, or a FREE_TEXT_AREA holding
    RESPONDENT_IDENTIFIER) ``{value}`` — trimmed, non-empty, at most 500;
  * FREE_TEXT ``{text}`` — trimmed, at most 5000, empty allowed;
  * CODE ``{value}`` — trimmed, non-empty, at most 2000.

The candidate
-------------
CONFIRM and SUBSTITUTE of a reading holding RESPONDENT_IDENTIFIER also set
``Submission.respondent_identifier``: CONFIRM to what was read (a grid's
``raw_value``, a free-text field's trimmed suggestion), SUBSTITUTE to the value
entered. FLAG and UNDO never touch it — the operator may have edited it by
hand, and "I cannot read this" is no reason to clear it.

Only the submission's FIRST identity reading in physical page order does so
(``review_rules.first_identifier_reading``), as at extraction: the linked
copies of a shared identifier and duplicate sheets give several readings, and
a ruling on a later one is recorded like any other but leaves the candidate
alone — no 409. With the first one flagged, no later one stands in for it.

Response
--------
The whole review page (``submission_review_service.get_review_detail``),
re-read after the commit, so the FE writes only what the server holds. The
number of statements a save runs does not depend on how many results the
submission holds.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.models.enums import ReviewAction, ReviewOutcome, SubmissionResultType
from app.models.page_object import PageObject
from app.models.submission import Submission
from app.models.submission_object_result import SubmissionObjectResult
from app.models.submission_review import SubmissionReview
from app.models.user import User
from app.repositories import submission_object_result_repo
from app.schemas.submission import (
    RESPONDENT_IDENTIFIER_MAX_LENGTH,
    ReviewActionType,
    SubmissionAcceptRemainingResult,
    SubmissionReviewActionRequest,
    SubmissionReviewDetail,
)
from app.schemas.submission_result import (
    CodeReviewedPayload,
    CodeSubstitution,
    FreeTextReviewedPayload,
    FreeTextSubstitution,
    MatrixColReview,
    MatrixReviewedPayload,
    MatrixRowReview,
    MatrixSubstitution,
    QuestionReviewedPayload,
    QuestionSubstitution,
    RespondentIdentifierReviewedPayload,
    RespondentIdentifierSubstitution,
)
from app.services import (
    audit_service,
    matrix_extraction_service,
    question_extraction_service,
    review_rules,
    submission_lock_service,
    submission_review_service,
)

# Machine-readable codes of this module's structured 409s.
NOT_CONFIRMABLE = "NOT_CONFIRMABLE"
NOT_RESOLVED = "NOT_RESOLVED"

# Length limits of the substituted values, after trimming.
FREE_TEXT_MAX_LENGTH = 5000
CODE_VALUE_MAX_LENGTH = 2000

# What each action leaves on the result, and what its log row says.
_OUTCOMES: dict[ReviewActionType, ReviewOutcome] = {
    ReviewActionType.CONFIRM: ReviewOutcome.CONFIRMED,
    ReviewActionType.SUBSTITUTE: ReviewOutcome.SUBSTITUTED,
    ReviewActionType.FLAG: ReviewOutcome.FLAGGED,
}
_LOG_ACTIONS: dict[ReviewActionType, ReviewAction] = {
    ReviewActionType.CONFIRM: ReviewAction.CONFIRMED,
    ReviewActionType.SUBSTITUTE: ReviewAction.SUBSTITUTED,
    ReviewActionType.FLAG: ReviewAction.FLAGGED,
    ReviewActionType.UNDO: ReviewAction.UNDONE,
}


def _now() -> datetime:
    """The instant a ruling is recorded at, timezone-aware UTC. One clock for
    the module, so tests freeze it by patching this."""
    return datetime.now(timezone.utc)


def _unprocessable(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=message
    )


# ---------------------------------------------------------------------------
# SUBSTITUTE: validating the value and building the reviewed payload
# ---------------------------------------------------------------------------


def _parse(model: type[BaseModel], value: dict[str, Any], expected: str):
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        raise _unprocessable(
            f"The value must be {expected} for this reading."
        ) from exc


def _question_payload(page_object: PageObject, value: dict[str, Any]) -> dict:
    data = _parse(QuestionSubstitution, value, "{selected_option_ids: [...]}")
    config = page_object.question_config
    if config is None:  # Defensive: every QUESTION object carries one.
        raise _unprocessable("This question has no options to select.")
    ids = data.selected_option_ids
    if len(set(ids)) != len(ids):
        raise _unprocessable("An option is selected more than once.")
    order = {option.id: option.order_index for option in config.options}
    unknown = [i for i in ids if i not in order]
    if unknown:
        raise _unprocessable(f"Option {unknown[0]} does not belong to this question.")
    selected = sorted(ids, key=lambda i: order[i])
    return QuestionReviewedPayload(
        selected_option_ids=selected,
        validity=question_extraction_service.compute_validity(
            config.question_type, len(selected)
        ),
    ).model_dump(mode="json")


def _matrix_payload(page_object: PageObject, value: dict[str, Any]) -> dict:
    data = _parse(MatrixSubstitution, value, "{selected_cells: [[row, col], ...]}")
    config = page_object.matrix_config
    if config is None:  # Defensive: every MATRIX object carries one.
        raise _unprocessable("This matrix has no cells to select.")
    rows_count, cols_count, rule = (
        config.rows_count,
        config.cols_count,
        config.selection_rule,
    )
    cells = set(data.selected_cells)
    if len(cells) != len(data.selected_cells):
        raise _unprocessable("A cell is selected more than once.")
    for row, col in data.selected_cells:
        if not (0 <= row < rows_count and 0 <= col < cols_count):
            raise _unprocessable(f"Cell [{row}, {col}] is outside the matrix.")

    # A degenerate matrix (no rows or no columns) has nothing to group — the
    # extractor writes the same empty payload for it.
    if rows_count <= 0 or cols_count <= 0:
        return MatrixReviewedPayload().model_dump(mode="json")
    compute = matrix_extraction_service.compute_group_validity
    if matrix_extraction_service.is_row_oriented(rule):
        rows = []
        for row in range(rows_count):
            selected = [col for col in range(cols_count) if (row, col) in cells]
            rows.append(
                MatrixRowReview(
                    row_index=row,
                    selected_col_indices=selected,
                    validity=compute(rule, len(selected)),
                )
            )
        return MatrixReviewedPayload(rows=rows).model_dump(mode="json")
    cols = []
    for col in range(cols_count):
        selected = [row for row in range(rows_count) if (row, col) in cells]
        cols.append(
            MatrixColReview(
                col_index=col,
                selected_row_indices=selected,
                validity=compute(rule, len(selected)),
            )
        )
    return MatrixReviewedPayload(cols=cols).model_dump(mode="json")


def _required_text(raw: str, max_length: int, what: str) -> str:
    text = raw.strip()
    if not text:
        raise _unprocessable(f"The {what} cannot be empty.")
    if len(text) > max_length:
        raise _unprocessable(f"The {what} is at most {max_length} characters.")
    return text


def _identifier_payload(value: dict[str, Any]) -> dict:
    data = _parse(RespondentIdentifierSubstitution, value, "{value: \"...\"}")
    return RespondentIdentifierReviewedPayload(
        value=_required_text(
            data.value, RESPONDENT_IDENTIFIER_MAX_LENGTH, "candidate identifier"
        )
    ).model_dump(mode="json")


def _free_text_payload(value: dict[str, Any]) -> dict:
    data = _parse(FreeTextSubstitution, value, "{text: \"...\"}")
    text = data.text.strip()
    if len(text) > FREE_TEXT_MAX_LENGTH:
        raise _unprocessable(f"The text is at most {FREE_TEXT_MAX_LENGTH} characters.")
    return FreeTextReviewedPayload(text=text).model_dump(mode="json")


def _code_payload(value: dict[str, Any]) -> dict:
    data = _parse(CodeSubstitution, value, "{value: \"...\"}")
    return CodeReviewedPayload(
        value=_required_text(data.value, CODE_VALUE_MAX_LENGTH, "code value")
    ).model_dump(mode="json")


def reviewed_payload(
    result: SubmissionObjectResult, page_object: PageObject, value: dict[str, Any] | None
) -> dict:
    """The reviewed payload a SUBSTITUTE of *result* with *value* writes, or a
    422 when *value* is missing or invalid for the reading's type (module
    docstring). A reading holding RESPONDENT_IDENTIFIER is an identity whatever
    its ``result_type``; a placeholder takes the shape of its ``result_type``."""
    if value is None:
        raise _unprocessable("SUBSTITUTE needs the value to substitute.")
    if review_rules.holds_respondent_identifier(result, page_object):
        return _identifier_payload(value)
    if result.result_type is SubmissionResultType.QUESTION:
        return _question_payload(page_object, value)
    if result.result_type is SubmissionResultType.MATRIX:
        return _matrix_payload(page_object, value)
    if result.result_type is SubmissionResultType.FREE_TEXT:
        return _free_text_payload(value)
    return _code_payload(value)


# ---------------------------------------------------------------------------
# One action on one reading
# ---------------------------------------------------------------------------


def _load_result(
    db: Session, submission_id: uuid.UUID, result_id: uuid.UUID
) -> SubmissionObjectResult:
    result = submission_object_result_repo.get_for_submission(
        db, submission_id, result_id
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Result not found"
        )
    return result


def _rule(
    result: SubmissionObjectResult,
    outcome: ReviewOutcome,
    payload: dict | None,
    note: str | None,
    user: User,
    now: datetime,
) -> None:
    result.review_outcome = outcome
    result.reviewed_payload_json = payload
    result.review_note = note
    result.reviewed_by = user.id
    result.reviewed_at = now


def _undo(result: SubmissionObjectResult) -> None:
    result.review_outcome = None
    result.reviewed_payload_json = None
    result.review_note = None
    result.reviewed_by = None
    result.reviewed_at = None


def _first_identifier_reading_id(
    db: Session, submission_id: uuid.UUID
) -> uuid.UUID | None:
    first = review_rules.first_identifier_reading(
        submission_object_result_repo.list_identifier_readings_for_submission(
            db, submission_id
        )
    )
    return first.id if first is not None else None


def _set_candidate(
    submission: Submission,
    result: SubmissionObjectResult,
    value: str,
    first_identifier_reading_id: uuid.UUID | None,
) -> None:
    # Called only for a reading holding RESPONDENT_IDENTIFIER: the first one
    # decides (module docstring, "The candidate").
    if result.id == first_identifier_reading_id:
        submission.respondent_identifier = value


def apply_action(
    db: Session,
    submission_id: uuid.UUID,
    result_id: uuid.UUID,
    user: User,
    request: SubmissionReviewActionRequest,
) -> SubmissionReviewDetail:
    """Apply one review action to one reading of a submission, and return the
    review page as it now stands. See the module docstring for the checks,
    their order, and what each action writes. Owns the transaction."""
    result = _load_result(db, submission_id, result_id)
    submission = submission_lock_service.guard_review_save(
        db, submission_id, user, request.review_version
    )
    page_object = result.page_object
    action = request.action
    if action is not ReviewActionType.SUBSTITUTE and request.value is not None:
        raise _unprocessable(f"{action.value} takes no value.")

    identity = review_rules.holds_respondent_identifier(result, page_object)
    payload: dict | None = None
    if action is ReviewActionType.CONFIRM:
        reason = review_rules.not_confirmable_reason(result, page_object)
        if reason is not None:
            raise submission_lock_service.structured_conflict(
                NOT_CONFIRMABLE,
                "This reading is incomplete and cannot be confirmed. "
                "Substitute the right value or flag it.",
                reason=reason,
            )
        if identity:
            _set_candidate(
                submission,
                result,
                review_rules.identifier_as_read(result),
                _first_identifier_reading_id(db, submission.id),
            )
    elif action is ReviewActionType.SUBSTITUTE:
        payload = reviewed_payload(result, page_object, request.value)
        if identity:
            _set_candidate(
                submission,
                result,
                payload["value"],
                _first_identifier_reading_id(db, submission.id),
            )
    elif action is ReviewActionType.UNDO and result.review_outcome is None:
        raise submission_lock_service.structured_conflict(
            NOT_RESOLVED, "This reading has not been reviewed: there is nothing to undo."
        )

    now = _now()
    if action is ReviewActionType.UNDO:
        _undo(result)
    else:
        _rule(result, _OUTCOMES[action], payload, request.note, user, now)

    submission_object_result_repo.add_review(
        db,
        SubmissionReview(
            submission_object_result_id=result.id,
            reviewer_user_id=user.id,
            action=_LOG_ACTIONS[action],
            review_note=request.note,
            reviewed_payload_json=payload,
            # The same instant as ``reviewed_at``, so the log row and the
            # ruling it records always agree on when it happened.
            created_at=now,
        ),
    )
    db.commit()
    return submission_review_service.get_review_detail(db, submission_id, user)


# ---------------------------------------------------------------------------
# "Accept all remaining"
# ---------------------------------------------------------------------------


def accept_remaining(
    db: Session, submission_id: uuid.UUID, user: User, declared_version: int
) -> SubmissionAcceptRemainingResult:
    """CONFIRM every unresolved, bulk-acceptable reading of a submission in one
    review save, and return the review page with how many were confirmed.

    ONE guard call, so ONE version bump however many readings it confirms —
    and still one when it confirms none: the call is a save like any other,
    and the response is then the page as it stands, with ``accepted_count`` 0.
    The readings are those listed by the review page (``review_required``)
    with ``review_outcome IS NULL`` and ``review_rules.is_bulk_acceptable``.
    Each gets a CONFIRMED log row with no note. A respondent-ID grid among
    them sets the candidate, as its single CONFIRM would: only the
    submission's first identity reading, whatever order they are walked in.

    Audited as ``submission.accepted_remaining`` with ``{accepted_count}``:
    one act touching many results at once, the case the per-action log alone
    does not tell as such. A constant number of statements, whatever the
    count. Owns the transaction.
    """
    submission = submission_lock_service.guard_review_save(
        db, submission_id, user, declared_version
    )
    eligible = [
        r
        for r in submission_object_result_repo.list_review_required_for_submission(
            db, submission.id
        )
        if r.review_outcome is None
        and review_rules.is_bulk_acceptable(r, r.page_object)
    ]
    identities = [
        r for r in eligible
        if review_rules.holds_respondent_identifier(r, r.page_object)
    ]
    if identities:
        first_id = _first_identifier_reading_id(db, submission.id)
        for result in identities:
            _set_candidate(
                submission, result, review_rules.identifier_as_read(result), first_id
            )
    submission_object_result_repo.confirm_many(
        db, [r.id for r in eligible], user.id, _now()
    )
    audit_service.log_event(
        db,
        actor_user_id=user.id,
        action=audit_service.SUBMISSION_ACCEPTED_REMAINING,
        entity_type="Submission",
        entity_id=submission.id,
        metadata={"accepted_count": len(eligible)},
    )
    db.commit()
    detail = submission_review_service.get_review_detail(db, submission_id, user)
    return SubmissionAcceptRemainingResult(
        **detail.model_dump(), accepted_count=len(eligible)
    )
