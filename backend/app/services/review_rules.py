# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which readings may be confirmed, and which "Accept all remaining" may
confirm unseen.

The ONE place these rules live. The review actions enforce them
(``review_action_service``) and the review read reports them per object
(``submission_review_service``, as ``confirmable`` / ``bulk_acceptable``), so
the FE never re-derives them and the two can never disagree. A module of its
own because both of those services need it and one of them already imports
the other.

Pure: they read the result and its PageObject (role binding loaded) and
nothing else — no session, no query — so the read can apply them to every
listed object at no cost.

Confirmable
-----------
CONFIRM says "the reading is right as read". It is refused when there is no
complete reading to be right, leaving SUBSTITUTE or FLAG:

  * :data:`NO_READING` — a placeholder ("Review anyway"): nothing was read;
  * :data:`EMPTY_IDENTIFIER` — a candidate identifier with nothing in it: a
    respondent-ID grid whose ``raw_value`` is empty (the degenerate grid), or a
    FREE_TEXT_AREA holding RESPONDENT_IDENTIFIER whose OCR suggestion is
    empty once trimmed;
  * :data:`INCOMPLETE_IDENTIFIER` — a respondent-ID grid with a column nothing
    was read in (its ``raw_value`` carries ``?``);
  * :data:`IDENTIFIER_TOO_LONG` — a free-text identifier whose suggestion would
    not fit the candidate column (500 characters). Confirming it would have to
    truncate the identity it is meant to attribute;
  * :data:`NOT_DECODED` — a CODE whose symbol did not decode.

Bulk acceptable
---------------
"Accept all remaining" confirms in one click what nobody looked at, so it
takes only readings a confirmation could not get wrong in a way that matters.
Confirmable, AND NOT:

  * a FREE_TEXT reading, whatever its role — handwriting is never accepted
    unseen (the same rule that makes it ALWAYS_REVIEW);
  * a respondent-ID grid with ``validity_forces_review`` — an ambiguous digit
    the system had to choose, an empty column (already excluded by
    "confirmable" too), or an unreadable grid: the candidate attribution is
    the most sensitive fact of a submission;
  * a reading on a page that could not be aligned (``sheet_not_aligned``):
    its readings are shifted systematically, so each must be checked against
    the sheet, one by one;
  * (already excluded by "confirmable") a placeholder or an undecoded CODE.

What stays is what a low confidence or an invalid combination sent to review
— exactly what an operator who has looked at the sheet and found nothing
wrong wants to accept wholesale.

Candidate from a reading
------------------------
A respondent ID read with no need for review becomes the submission's
candidate. :func:`candidate_from_reading` says which reading qualifies, and
with what value: a respondent-ID GRID (a free-text identifier is
always reviewed, so never qualifies), not ``review_required``, with no human
ruling on it, with no ambiguous column, and confirmable — the same conditions
as CONFIRM, so a candidate set automatically is always a value an operator
could have confirmed. The ambiguity is checked on the payload and not only
through ``review_required``: a reading stored before an ambiguous column forced
review has ``ambiguous_columns`` set and ``validity_forces_review`` False, and
nothing migrated it (see ``respondent_identifier_extraction_service``). When
and on which submission it is applied is ``submission_candidate_service``'s
rule.

The first identity reading
--------------------------
A submission may hold several readings of its identifier: the linked copies
of a shared respondent ID or free text, or duplicate sheets. Only the FIRST in
physical page order speaks for the candidate, at extraction and in review
alike: :func:`first_identifier_reading` says which one it is, whatever its
review state — a first reading flagged or in review is still the first, and no
later one takes its place. It looks at grids and free texts alike, where the
extraction's candidate reads grids only
(``submission_object_result_repo.list_respondent_identifier_readings``): one
variant binds RESPONDENT_IDENTIFIER to one object, so a submission's identity
readings are all of one kind and the two agree on which is first.
"""
from __future__ import annotations

from collections.abc import Iterable

from app.models.enums import PageObjectRole, SubmissionResultType
from app.models.page_object import PageObject
from app.models.submission_object_result import SubmissionObjectResult
from app.schemas.submission import RESPONDENT_IDENTIFIER_MAX_LENGTH

# ``reason`` values of the NOT_CONFIRMABLE structured 409 (see the docstring).
NO_READING = "NO_READING"
EMPTY_IDENTIFIER = "EMPTY_IDENTIFIER"
INCOMPLETE_IDENTIFIER = "INCOMPLETE_IDENTIFIER"
IDENTIFIER_TOO_LONG = "IDENTIFIER_TOO_LONG"
NOT_DECODED = "NOT_DECODED"

# The character a respondent-ID grid writes for a column with no selection
# (``RespondentIdentifierDetectionPayload.raw_value``).
UNREAD_DIGIT = "?"


def holds_respondent_identifier(
    result: SubmissionObjectResult, page_object: PageObject
) -> bool:
    """Whether *result* reads a candidate identifier: a respondent-ID grid, or a
    FREE_TEXT_AREA holding the RESPONDENT_IDENTIFIER role.

    Such a reading is reviewed as an identity (``{value}``) rather than as an
    answer, and confirming or substituting it sets the submission's candidate.
    """
    return (
        result.result_type is SubmissionResultType.RESPONDENT_IDENTIFIER
        or page_object.effective_role is PageObjectRole.RESPONDENT_IDENTIFIER
    )


def identifier_as_read(result: SubmissionObjectResult) -> str:
    """The candidate identifier the reading of *result* spells, as a CONFIRM
    would record it: a grid's ``raw_value``, a free-text field's trimmed
    suggestion. Empty when nothing was read."""
    payload = result.detected_payload_json or {}
    if result.result_type is SubmissionResultType.RESPONDENT_IDENTIFIER:
        return payload.get("raw_value") or ""
    return (payload.get("suggested_text") or "").strip()


def not_confirmable_reason(
    result: SubmissionObjectResult, page_object: PageObject
) -> str | None:
    """Why *result* cannot be CONFIRMED, or None when it can (module docstring)."""
    payload = result.detected_payload_json
    if payload is None:
        return NO_READING
    if holds_respondent_identifier(result, page_object):
        value = identifier_as_read(result)
        if not value.strip():
            return EMPTY_IDENTIFIER
        if (
            result.result_type is SubmissionResultType.RESPONDENT_IDENTIFIER
            and UNREAD_DIGIT in value
        ):
            return INCOMPLETE_IDENTIFIER
        if len(value) > RESPONDENT_IDENTIFIER_MAX_LENGTH:
            return IDENTIFIER_TOO_LONG
        return None
    if result.result_type is SubmissionResultType.CODE and not payload.get(
        "decode_successful"
    ):
        return NOT_DECODED
    return None


def is_confirmable(result: SubmissionObjectResult, page_object: PageObject) -> bool:
    """Whether CONFIRM is allowed on *result* (module docstring)."""
    return not_confirmable_reason(result, page_object) is None


def candidate_from_reading(
    result: SubmissionObjectResult, page_object: PageObject
) -> str | None:
    """The candidate identifier *result* sets with no review, or None when it
    sets none (module docstring, "Candidate from a reading")."""
    if result.result_type is not SubmissionResultType.RESPONDENT_IDENTIFIER:
        return None
    if result.review_required or result.review_outcome is not None:
        return None
    if (result.detected_payload_json or {}).get("ambiguous_columns"):
        return None
    if not is_confirmable(result, page_object):
        return None
    return identifier_as_read(result)


def first_identifier_reading(
    results: Iterable[SubmissionObjectResult],
) -> SubmissionObjectResult | None:
    """The first of *results* (one submission's, with sheet and PageObject
    loaded) that reads a candidate identifier, in physical page order and then
    by id, the order of the extraction's respondent-ID readings; None when none
    does (module docstring, "The first identity reading")."""
    identities = [
        r for r in results if holds_respondent_identifier(r, r.page_object)
    ]
    if not identities:
        return None
    return min(identities, key=lambda r: (r.submission_page.source_page_order, r.id))


def is_bulk_acceptable(
    result: SubmissionObjectResult, page_object: PageObject
) -> bool:
    """Whether "Accept all remaining" may confirm *result* unseen (module
    docstring). A rule on the READING only: whether the result is still
    unresolved and in review is the caller's filter, not part of this."""
    if not is_confirmable(result, page_object):
        return False
    if result.result_type is SubmissionResultType.FREE_TEXT:
        return False
    if result.sheet_not_aligned:
        return False
    if (
        result.result_type is SubmissionResultType.RESPONDENT_IDENTIFIER
        and result.validity_forces_review
    ):
        return False
    return True
