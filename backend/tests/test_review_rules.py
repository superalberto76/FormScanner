# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure unit tests for ``review_rules.candidate_from_reading`` and
``review_rules.first_identifier_reading``.

The rule that says which reading becomes a submission's candidate with no
review: a respondent-ID grid, out of review, with no human ruling, confirmable.
No DB: the rule reads the result and its PageObject and nothing else, so
transient ORM objects are enough. When and on which submission it applies is
tested end-to-end in
``tests/integration/test_submission_candidate_from_reading_integration.py``;
the review side of the first reading in
``tests/integration/test_identifier_first_reading_integration.py``.
"""
from __future__ import annotations

import uuid

import pytest

from app.models.enums import (
    PageObjectRole,
    PageObjectType,
    ReviewOutcome,
    SubmissionResultType,
)
from app.models.form_variant_role_binding import FormVariantRoleBinding
from app.models.page_object import PageObject
from app.models.submission_object_result import SubmissionObjectResult
from app.models.submission_page import SubmissionPage
from app.schemas.submission import RESPONDENT_IDENTIFIER_MAX_LENGTH
from app.services.review_rules import candidate_from_reading, first_identifier_reading


def _object(
    object_type: PageObjectType = PageObjectType.MATRIX,
    role: PageObjectRole | None = PageObjectRole.RESPONDENT_IDENTIFIER,
) -> PageObject:
    obj = PageObject(object_type=object_type, x=0, y=0, width=10, height=10)
    if role is not None:
        obj.role_binding = FormVariantRoleBinding(role=role)
    return obj


def _respondent_id_result(
    raw_value: str | None = "37095",
    *,
    review_required: bool = False,
    review_outcome: ReviewOutcome | None = None,
) -> SubmissionObjectResult:
    return SubmissionObjectResult(
        result_type=SubmissionResultType.RESPONDENT_IDENTIFIER,
        detected_payload_json=(
            None if raw_value is None else {"raw_value": raw_value}
        ),
        review_required=review_required,
        review_outcome=review_outcome,
    )


def test_a_clean_respondent_id_out_of_review_is_the_candidate():
    assert candidate_from_reading(_respondent_id_result(), _object()) == "37095"


def test_a_respondent_id_in_review_is_not():
    assert (
        candidate_from_reading(_respondent_id_result(review_required=True), _object())
        is None
    )


@pytest.mark.parametrize("outcome", list(ReviewOutcome))
def test_a_respondent_id_a_human_ruled_on_is_not(outcome):
    """A ruling already decided the candidate (CONFIRM / SUBSTITUTE) or said
    the reading is wrong (FLAG): the reading no longer speaks for itself."""
    assert (
        candidate_from_reading(_respondent_id_result(review_outcome=outcome), _object())
        is None
    )


@pytest.mark.parametrize(
    "raw_value",
    [
        "37?95",  # a column with no mark
        "",  # the degenerate grid
        None,  # a placeholder ("Review anyway")
        "1" * (RESPONDENT_IDENTIFIER_MAX_LENGTH + 1),  # does not fit the column
    ],
)
def test_a_respondent_id_that_could_not_be_confirmed_is_not(raw_value):
    assert candidate_from_reading(_respondent_id_result(raw_value), _object()) is None


def test_a_free_text_identifier_is_never_the_candidate_unseen():
    """A FREE_TEXT_AREA holding RESPONDENT_IDENTIFIER is always reviewed; even
    a reading flagged out of review would not qualify."""
    result = SubmissionObjectResult(
        result_type=SubmissionResultType.FREE_TEXT,
        detected_payload_json={"suggested_text": "Mario Rossi"},
        review_required=False,
    )
    obj = _object(PageObjectType.FREE_TEXT_AREA)
    assert candidate_from_reading(result, obj) is None


def test_an_ordinary_reading_is_never_the_candidate():
    result = SubmissionObjectResult(
        result_type=SubmissionResultType.MATRIX,
        detected_payload_json={"raw_value": "37095"},
        review_required=False,
    )
    assert candidate_from_reading(result, _object(role=None)) is None


def test_a_reading_with_an_ambiguous_column_is_never_the_candidate():
    """A reading stored before an ambiguous column forced review is out of
    review (``validity_forces_review`` False) and complete, yet one digit was
    the system's choice: it must not attribute the submission unseen."""
    result = _respondent_id_result()
    result.detected_payload_json = {"raw_value": "37095", "ambiguous_columns": [2]}
    assert candidate_from_reading(result, _object()) is None


# ---------------------------------------------------------------------------
# The first identity reading of a submission
# ---------------------------------------------------------------------------


def _on_sheet(
    result: SubmissionObjectResult,
    sheet_order: int,
    obj: PageObject | None = None,
    result_id: int = 0,
) -> SubmissionObjectResult:
    result.id = uuid.UUID(int=result_id)
    result.submission_page = SubmissionPage(source_page_order=sheet_order)
    result.page_object = obj if obj is not None else _object()
    return result


def test_the_first_identity_reading_is_the_one_on_the_first_sheet():
    later = _on_sheet(_respondent_id_result("200002"), 2, result_id=1)
    first = _on_sheet(_respondent_id_result("100001"), 0, result_id=2)
    middle = _on_sheet(_respondent_id_result("300003"), 1, result_id=3)
    assert first_identifier_reading([later, first, middle]) is first


def test_its_review_state_does_not_matter():
    """A first reading in review, or flagged, is still the first: the later
    ones never take its place."""
    first = _on_sheet(
        _respondent_id_result("1?0001", review_outcome=ReviewOutcome.FLAGGED),
        0,
        result_id=9,
    )
    later = _on_sheet(_respondent_id_result("200002"), 1, result_id=1)
    assert first_identifier_reading([later, first]) is first


def test_readings_that_are_not_an_identity_are_ignored():
    answer = _on_sheet(
        SubmissionObjectResult(
            result_type=SubmissionResultType.FREE_TEXT,
            detected_payload_json={"suggested_text": "Some words"},
        ),
        0,
        _object(PageObjectType.FREE_TEXT_AREA, role=None),
    )
    identity = _on_sheet(
        SubmissionObjectResult(
            result_type=SubmissionResultType.FREE_TEXT,
            detected_payload_json={"suggested_text": "Mario Rossi"},
        ),
        1,
        _object(PageObjectType.FREE_TEXT_AREA),
        result_id=1,
    )
    assert first_identifier_reading([answer, identity]) is identity
    assert first_identifier_reading([answer]) is None
    assert first_identifier_reading([]) is None


def test_on_one_sheet_the_result_id_breaks_the_tie():
    """Never two identity readings on one sheet in practice (one object per
    role per variant); the order still has to be total, like the extraction's."""
    high = _on_sheet(_respondent_id_result("200002"), 0, result_id=7)
    low = _on_sheet(_respondent_id_result("100001"), 0, result_id=3)
    assert first_identifier_reading([high, low]) is low
