# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure unit tests for ``scoring_rules``.

The score of one QUESTION reading (``score_question``) and which reading is
scored (``question_reading``); the same for a MATRIX (``score_matrix``,
``matrix_reading``) and which MATRIX counts (``is_scored_matrix``); the
"no answer" cases and the score of a whole result (``unanswered``,
``score_question_result``, ``score_matrix_result``); the maximum score
(``question_max_score``, ``matrix_max_score``, ``variant_max_score``);
the score of a submission (``submission_score``).
No DB: the rules read the authored options and the result and nothing else,
so transient ORM objects are enough.
"""
from __future__ import annotations

import itertools
import uuid

import pytest
from pydantic import ValidationError

from app.models.enums import (
    MatrixSelectionRule,
    PageObjectRole,
    PageObjectType,
    QuestionType,
    ReviewOutcome,
    SubmissionResultType,
    Validity,
)
from app.models.form_variant_role_binding import FormVariantRoleBinding
from app.models.matrix_cell_def import MatrixCellDef
from app.models.matrix_object_config import MatrixObjectConfig
from app.models.page_object import PageObject
from app.models.question_object_config import QuestionObjectConfig
from app.models.question_option import QuestionOption
from app.models.submission_object_result import SubmissionObjectResult
from app.models.submission_page import SubmissionPage
from app.services.scoring_rules import (
    MatrixGroupReading,
    MatrixGroupScore,
    MatrixReading,
    MatrixResultScore,
    MatrixScore,
    QuestionReading,
    QuestionResultScore,
    QuestionScore,
    SubmissionScore,
    Unanswered,
    _max_selection,
    _score_selection,
    is_scored_matrix,
    matrix_max_score,
    matrix_reading,
    question_max_score,
    question_reading,
    score_matrix,
    score_matrix_result,
    score_question,
    score_question_result,
    submission_score,
    unanswered,
    first_readings,
    reading_key,
    variant_max_score,
)
from app.services.question_extraction_service import compute_validity


def _options(*scores: float | None) -> list[QuestionOption]:
    return [
        QuestionOption(
            id=uuid.uuid4(),
            order_index=index,
            option_text=f"option {index}",
            is_correct=score is not None and score > 0,
            score=score,
        )
        for index, score in enumerate(scores)
    ]


def _ids(options: list[QuestionOption], *indices: int) -> list[uuid.UUID]:
    return [options[index].id for index in indices]


# ---------------------------------------------------------------------------
# score_question
# ---------------------------------------------------------------------------


def test_a_valid_correct_answer_scores_its_option():
    options = _options(2, 0, -1, 0)
    assert score_question(options, _ids(options, 0), Validity.VALID) == QuestionScore(
        raw_score=2.0, score=2.0, full_answer=False
    )


@pytest.mark.parametrize(("index", "expected"), [(1, 0.0), (2, -1.0)])
def test_a_valid_wrong_answer_scores_its_option_even_negative(index, expected):
    options = _options(2, 0, -1, 0)
    result = score_question(options, _ids(options, index), Validity.VALID)
    assert result.score == expected


def test_a_valid_partial_multi_choice_answer_scores_the_algebraic_sum():
    options = _options(1, 1, -0.5, 1)
    result = score_question(options, _ids(options, 0, 1, 2), Validity.VALID)
    assert result == QuestionScore(raw_score=1.5, score=1.5, full_answer=False)


def test_no_answer_scores_zero():
    options = _options(2, 0, -1, 0)
    assert score_question(options, [], Validity.NO_ANSWER) == QuestionScore(
        raw_score=0.0, score=0.0, full_answer=False
    )


def test_too_many_answers_never_scores_positive():
    options = _options(2, 0, -1, 0)
    result = score_question(options, _ids(options, 0, 1), Validity.TOO_MANY_ANSWERS)
    assert result == QuestionScore(raw_score=2.0, score=0.0, full_answer=False)


def test_too_many_answers_keeps_a_negative_raw_score():
    options = _options(2, 0, -1, -1)
    result = score_question(options, _ids(options, 2, 3), Validity.TOO_MANY_ANSWERS)
    assert result.score == -2.0


@pytest.mark.parametrize(
    "validity",
    [Validity.TOO_MANY_ANSWERS, Validity.VALID],
    ids=["single-choice", "multi-choice"],
)
def test_every_option_selected_is_a_full_answer_worth_zero(validity):
    """Extraction leaves it TOO_MANY_ANSWERS on a single-choice question and
    VALID on a multi-choice one: scoring recognises it either way, and gives 0
    even where min(0, raw_score) would be negative."""
    options = _options(1, -1, -1, -1)
    result = score_question(options, _ids(options, 0, 1, 2, 3), validity)
    assert result == QuestionScore(raw_score=-2.0, score=0.0, full_answer=True)


def test_a_positive_full_answer_is_worth_zero_too():
    options = _options(1, 1)
    result = score_question(options, _ids(options, 0, 1), Validity.VALID)
    assert result == QuestionScore(raw_score=2.0, score=0.0, full_answer=True)


def test_the_only_option_of_a_question_is_an_answer_not_a_full_answer():
    """With one option there is nothing to choose between."""
    options = _options(3)
    result = score_question(options, _ids(options, 0), Validity.VALID)
    assert result == QuestionScore(raw_score=3.0, score=3.0, full_answer=False)


def test_an_option_with_no_score_is_worth_zero_even_when_correct():
    """``is_correct`` does not take part in scoring."""
    options = _options(None, 1, 0)
    options[0].is_correct = True
    result = score_question(options, _ids(options, 0), Validity.VALID)
    assert result == QuestionScore(raw_score=0.0, score=0.0, full_answer=False)


def test_a_full_answer_with_unscored_options_is_still_a_full_answer():
    options = _options(None, None)
    result = score_question(options, _ids(options, 0, 1), Validity.VALID)
    assert result.full_answer is True
    assert result.score == 0.0


def test_a_selected_id_listed_twice_counts_once():
    options = _options(2, 0, 0)
    result = score_question(options, _ids(options, 0, 0), Validity.VALID)
    assert result.raw_score == 2.0


def test_one_option_of_two_is_not_a_full_answer():
    options = _options(1, -1)
    result = score_question(options, _ids(options, 0), Validity.VALID)
    assert result == QuestionScore(raw_score=1.0, score=1.0, full_answer=False)


def test_too_many_answers_on_unscored_options_scores_zero():
    options = _options(None, None, None)
    result = score_question(options, _ids(options, 0, 1), Validity.TOO_MANY_ANSWERS)
    assert result == QuestionScore(raw_score=0.0, score=0.0, full_answer=False)


def test_a_validity_that_is_not_a_member_is_refused():
    """A raw string is not scored by elimination as TOO_MANY_ANSWERS."""
    options = _options(2, 0)
    with pytest.raises(ValueError, match="not a Validity"):
        score_question(options, _ids(options, 0), "VALID")


def test_a_selected_id_not_in_the_question_is_refused():
    options = _options(2, 0)
    with pytest.raises(ValueError, match="not in the question"):
        score_question(options, [uuid.uuid4()], Validity.VALID)


# ---------------------------------------------------------------------------
# question_reading
# ---------------------------------------------------------------------------


def _result(
    *,
    detected: dict | None,
    reviewed: dict | None = None,
    outcome: ReviewOutcome | None = None,
    result_type: SubmissionResultType = SubmissionResultType.QUESTION,
    review_required: bool = False,
) -> SubmissionObjectResult:
    return SubmissionObjectResult(
        result_type=result_type,
        detected_payload_json=detected,
        reviewed_payload_json=reviewed,
        review_outcome=outcome,
        review_required=review_required,
    )


def _detected(*option_ids: uuid.UUID, validity: Validity) -> dict:
    return {
        "selected_options": [
            {"option_id": str(option_id), "confidence": 0.9} for option_id in option_ids
        ],
        "validity": validity.value,
    }


A, B = uuid.uuid4(), uuid.uuid4()


@pytest.mark.parametrize("outcome", [None, ReviewOutcome.CONFIRMED])
def test_an_unreviewed_or_confirmed_result_scores_the_extracted_reading(outcome):
    result = _result(detected=_detected(A, B, validity=Validity.TOO_MANY_ANSWERS), outcome=outcome)
    assert question_reading(result) == QuestionReading(
        selected_option_ids=frozenset({A, B}), validity=Validity.TOO_MANY_ANSWERS
    )


def test_a_substituted_result_scores_the_operators_value():
    result = _result(
        detected=_detected(A, B, validity=Validity.TOO_MANY_ANSWERS),
        reviewed={"selected_option_ids": [str(B)], "validity": "VALID"},
        outcome=ReviewOutcome.SUBSTITUTED,
    )
    assert question_reading(result) == QuestionReading(
        selected_option_ids=frozenset({B}), validity=Validity.VALID
    )


def test_a_substituted_placeholder_scores_the_operators_value():
    result = _result(
        detected=None,
        reviewed={"selected_option_ids": [], "validity": "NO_ANSWER"},
        outcome=ReviewOutcome.SUBSTITUTED,
    )
    assert question_reading(result) == QuestionReading(
        selected_option_ids=frozenset(), validity=Validity.NO_ANSWER
    )


def test_a_placeholder_no_one_substituted_has_no_reading():
    assert question_reading(_result(detected=None)) is None


def test_a_flagged_result_still_returns_its_reading():
    """The "no answer" rules belong to ``unanswered``, not to this function."""
    result = _result(detected=_detected(A, validity=Validity.VALID), outcome=ReviewOutcome.FLAGGED)
    assert question_reading(result) == QuestionReading(
        selected_option_ids=frozenset({A}), validity=Validity.VALID
    )


def test_a_substituted_result_without_its_payload_is_refused():
    result = _result(detected=_detected(A, validity=Validity.VALID), outcome=ReviewOutcome.SUBSTITUTED)
    with pytest.raises(ValidationError):
        question_reading(result)


def test_a_result_that_is_not_a_question_is_refused():
    result = _result(detected={}, result_type=SubmissionResultType.MATRIX)
    with pytest.raises(ValueError, match="not a QUESTION"):
        question_reading(result)


# ---------------------------------------------------------------------------
# score_matrix
# ---------------------------------------------------------------------------


def _cells(scores: dict[tuple[int, int], float | None]) -> list[MatrixCellDef]:
    return [
        MatrixCellDef(
            row_index=row,
            col_index=col,
            is_correct=score is not None and score > 0,
            score=score,
        )
        for (row, col), score in scores.items()
    ]


def _rows(*groups: tuple[int, set[int], Validity]) -> MatrixReading:
    return MatrixReading(
        row_oriented=True,
        groups=tuple(MatrixGroupReading(i, frozenset(sel), v) for i, sel, v in groups),
    )


def _cols(*groups: tuple[int, set[int], Validity]) -> MatrixReading:
    return MatrixReading(
        row_oriented=False,
        groups=tuple(MatrixGroupReading(i, frozenset(sel), v) for i, sel, v in groups),
    )


# A 3x3 ONE_PER_ROW grid with the diagonal correct (+1) and one wrong cell
# worth -1; every other cell is not authored.
DIAGONAL = _cells({(0, 0): 1, (1, 1): 1, (2, 2): 1, (0, 1): -1})


def test_each_row_is_scored_and_the_matrix_is_their_algebraic_sum():
    reading = _rows(
        (0, {0}, Validity.VALID),
        (1, {1}, Validity.VALID),
        (2, {0}, Validity.VALID),
    )
    assert score_matrix(3, 3, DIAGONAL, reading) == MatrixScore(
        score=2.0,
        groups=(
            MatrixGroupScore(0, raw_score=1.0, score=1.0, full_answer=False),
            MatrixGroupScore(1, raw_score=1.0, score=1.0, full_answer=False),
            MatrixGroupScore(2, raw_score=0.0, score=0.0, full_answer=False),
        ),
    )


def test_a_negative_row_lowers_the_sum():
    reading = _rows(
        (0, {1}, Validity.VALID),
        (1, {1}, Validity.VALID),
        (2, set(), Validity.NO_ANSWER),
    )
    assert score_matrix(3, 3, DIAGONAL, reading).score == 0.0


def test_too_many_answers_in_a_row_never_scores_positive():
    reading = _rows(
        (0, {0, 2}, Validity.TOO_MANY_ANSWERS),
        (1, set(), Validity.NO_ANSWER),
        (2, set(), Validity.NO_ANSWER),
    )
    result = score_matrix(3, 3, DIAGONAL, reading)
    assert result.groups[0] == MatrixGroupScore(0, raw_score=1.0, score=0.0, full_answer=False)
    assert result.score == 0.0


def test_too_many_answers_in_a_row_keeps_a_negative_raw_score():
    reading = _rows((0, {1, 2}, Validity.TOO_MANY_ANSWERS))
    assert score_matrix(3, 3, DIAGONAL, reading).score == -1.0


def test_every_bubble_of_a_single_choice_row_is_a_full_answer_worth_zero():
    reading = _rows((0, {0, 1, 2}, Validity.TOO_MANY_ANSWERS))
    assert score_matrix(3, 3, DIAGONAL, reading).groups[0] == MatrixGroupScore(
        0, raw_score=0.0, score=0.0, full_answer=True
    )


def test_a_multi_selection_row_scores_a_partial_answer_and_zeroes_a_full_one():
    """MANY_PER_ROW and FREE leave every selection VALID: the sum counts, until
    every bubble of the row is marked. The rule only shows in the validity."""
    cells = _cells({(0, 0): 1, (0, 1): -2, (0, 2): 1})
    partial = score_matrix(1, 3, cells, _rows((0, {0, 2}, Validity.VALID)))
    full = score_matrix(1, 3, cells, _rows((0, {0, 1, 2}, Validity.VALID)))
    assert partial.score == 2.0
    assert full.groups[0] == MatrixGroupScore(0, raw_score=0.0, score=0.0, full_answer=True)


def test_a_column_rule_scores_each_column():
    """Under ONE_PER_COLUMN the column is the mini-QUESTION."""
    cells = _cells({(2, 0): 1, (1, 1): 1, (0, 2): 1, (0, 0): -1})
    reading = _cols(
        (0, {2}, Validity.VALID),
        (1, {0}, Validity.VALID),
        (2, {0}, Validity.VALID),
    )
    assert score_matrix(3, 3, cells, reading) == MatrixScore(
        score=2.0,
        groups=(
            MatrixGroupScore(0, raw_score=1.0, score=1.0, full_answer=False),
            MatrixGroupScore(1, raw_score=0.0, score=0.0, full_answer=False),
            MatrixGroupScore(2, raw_score=1.0, score=1.0, full_answer=False),
        ),
    )


def test_a_multi_selection_column_scores_a_partial_answer():
    """MANY_PER_COLUMN: the selected rows of a column sum like a multi-choice."""
    cells = _cells({(0, 0): 1, (1, 0): -0.5, (2, 0): 1})
    reading = _cols((0, {0, 1}, Validity.VALID), (1, set(), Validity.NO_ANSWER))
    assert score_matrix(3, 2, cells, reading) == MatrixScore(
        score=0.5,
        groups=(
            MatrixGroupScore(0, raw_score=0.5, score=0.5, full_answer=False),
            MatrixGroupScore(1, raw_score=0.0, score=0.0, full_answer=False),
        ),
    )


def test_a_column_full_answer_counts_the_rows_of_the_grid():
    cells = _cells({(0, 0): 1})
    reading = _cols((0, {0, 1, 2, 3}, Validity.TOO_MANY_ANSWERS))
    result = score_matrix(4, 2, cells, reading)
    assert result.groups[0].full_answer is True


def test_a_row_with_one_authored_cell_is_not_a_full_answer_when_only_that_is_marked():
    """The options of a row are its printed bubbles, not its authored cells."""
    cells = _cells({(0, 1): 2})
    result = score_matrix(1, 4, cells, _rows((0, {1}, Validity.VALID)))
    assert result.groups[0] == MatrixGroupScore(0, raw_score=2.0, score=2.0, full_answer=False)


def test_every_printed_bubble_of_a_sparse_row_is_a_full_answer():
    cells = _cells({(0, 1): 2})
    result = score_matrix(1, 4, cells, _rows((0, {0, 1, 2, 3}, Validity.VALID)))
    assert result.groups[0] == MatrixGroupScore(0, raw_score=2.0, score=0.0, full_answer=True)


def test_the_only_bubble_of_a_one_column_row_is_an_answer_not_a_full_answer():
    cells = _cells({(0, 0): 1})
    result = score_matrix(1, 1, cells, _rows((0, {0}, Validity.VALID)))
    assert result.groups[0] == MatrixGroupScore(0, raw_score=1.0, score=1.0, full_answer=False)


def test_a_cell_with_no_score_is_worth_zero_even_when_correct():
    cells = _cells({(0, 0): None})
    cells[0].is_correct = True
    assert score_matrix(1, 3, cells, _rows((0, {0}, Validity.VALID))).score == 0.0


def test_a_degenerate_matrix_scores_zero():
    assert score_matrix(0, 3, [], _rows()) == MatrixScore(score=0.0, groups=())


def test_a_group_missing_from_the_reading_contributes_nothing():
    result = score_matrix(3, 3, DIAGONAL, _rows((1, {1}, Validity.VALID)))
    assert result == MatrixScore(
        score=1.0, groups=(MatrixGroupScore(1, raw_score=1.0, score=1.0, full_answer=False),)
    )


@pytest.mark.parametrize(
    "reading",
    [
        _rows((3, {0}, Validity.VALID)),
        _rows((-1, {0}, Validity.VALID)),
        _rows((0, {3}, Validity.VALID)),
        _cols((0, {3}, Validity.VALID)),
    ],
    ids=["row-index", "negative-row", "selected-col", "selected-row-in-column"],
)
def test_an_index_outside_the_grid_is_refused(reading):
    with pytest.raises(ValueError, match="outside a 3x3 grid"):
        score_matrix(3, 3, DIAGONAL, reading)


def test_a_group_listed_twice_is_refused():
    reading = _rows((0, {0}, Validity.VALID), (0, {0}, Validity.VALID))
    with pytest.raises(ValueError, match="listed twice"):
        score_matrix(3, 3, DIAGONAL, reading)


def test_a_group_validity_that_is_not_a_member_is_refused():
    reading = _rows((0, {0}, "VALID"))
    with pytest.raises(ValueError, match="not a Validity"):
        score_matrix(3, 3, DIAGONAL, reading)


# ---------------------------------------------------------------------------
# matrix_reading
# ---------------------------------------------------------------------------


def _matrix_result(**kwargs) -> SubmissionObjectResult:
    return _result(result_type=SubmissionResultType.MATRIX, **kwargs)


ROWS_DETECTED = {
    "rows": [
        {"row_index": 0, "selected_cells": [{"col_index": 1, "confidence": 0.9}], "validity": "VALID"},
        {
            "row_index": 1,
            "selected_cells": [
                {"col_index": 0, "confidence": 0.8},
                {"col_index": 2, "confidence": 0.7},
            ],
            "validity": "TOO_MANY_ANSWERS",
        },
    ]
}
COLS_DETECTED = {
    "cols": [
        {"col_index": 0, "selected_cells": [{"row_index": 2, "confidence": 0.9}], "validity": "VALID"},
        {"col_index": 1, "selected_cells": [], "validity": "NO_ANSWER"},
    ]
}


@pytest.mark.parametrize("outcome", [None, ReviewOutcome.CONFIRMED])
def test_an_unreviewed_or_confirmed_matrix_scores_the_extracted_rows(outcome):
    assert matrix_reading(_matrix_result(detected=ROWS_DETECTED, outcome=outcome)) == _rows(
        (0, {1}, Validity.VALID),
        (1, {0, 2}, Validity.TOO_MANY_ANSWERS),
    )


def test_an_extracted_column_reading_is_column_oriented():
    assert matrix_reading(_matrix_result(detected=COLS_DETECTED)) == _cols(
        (0, {2}, Validity.VALID),
        (1, set(), Validity.NO_ANSWER),
    )


def test_a_substituted_matrix_scores_the_operators_rows():
    result = _matrix_result(
        detected=ROWS_DETECTED,
        reviewed={
            "rows": [
                {"row_index": 0, "selected_col_indices": [1], "validity": "VALID"},
                {"row_index": 1, "selected_col_indices": [2], "validity": "VALID"},
            ]
        },
        outcome=ReviewOutcome.SUBSTITUTED,
    )
    assert matrix_reading(result) == _rows((0, {1}, Validity.VALID), (1, {2}, Validity.VALID))


def test_a_substituted_matrix_scores_the_operators_columns():
    result = _matrix_result(
        detected=COLS_DETECTED,
        reviewed={
            "cols": [
                {"col_index": 0, "selected_row_indices": [2], "validity": "VALID"},
                {"col_index": 1, "selected_row_indices": [0], "validity": "VALID"},
            ]
        },
        outcome=ReviewOutcome.SUBSTITUTED,
    )
    assert matrix_reading(result) == _cols((0, {2}, Validity.VALID), (1, {0}, Validity.VALID))


def test_a_matrix_placeholder_no_one_substituted_has_no_reading():
    assert matrix_reading(_matrix_result(detected=None)) is None


def test_a_matrix_reading_with_both_rows_and_columns_is_refused():
    with pytest.raises(ValueError, match="both rows and columns"):
        matrix_reading(_matrix_result(detected={**ROWS_DETECTED, **COLS_DETECTED}))


@pytest.mark.parametrize(
    "result_type",
    [SubmissionResultType.QUESTION, SubmissionResultType.RESPONDENT_IDENTIFIER],
)
def test_a_result_that_is_not_a_matrix_is_refused(result_type):
    with pytest.raises(ValueError, match="not a MATRIX"):
        matrix_reading(_result(detected={}, result_type=result_type))


def test_the_extracted_reading_scores_end_to_end():
    reading = matrix_reading(_matrix_result(detected=ROWS_DETECTED))
    assert score_matrix(3, 3, DIAGONAL, reading).score == -1.0


# ---------------------------------------------------------------------------
# is_scored_matrix
# ---------------------------------------------------------------------------


def _matrix_object(
    *, evaluative: bool, role: PageObjectRole | None = None
) -> PageObject:
    page_object = PageObject(object_type=PageObjectType.MATRIX)
    page_object.matrix_config = MatrixObjectConfig(
        rows_count=3,
        cols_count=3,
        evaluative=evaluative,
        selection_rule=MatrixSelectionRule.ONE_PER_ROW,
    )
    if role is not None:
        page_object.role_binding = FormVariantRoleBinding(role=role)
    return page_object


def test_an_evaluative_matrix_is_scored():
    assert is_scored_matrix(_matrix_object(evaluative=True)) is True


def test_a_matrix_that_is_not_evaluative_is_not_scored():
    """The authoring flag decides."""
    assert is_scored_matrix(_matrix_object(evaluative=False)) is False


def test_a_respondent_id_grid_is_not_scored_even_when_flagged_evaluative():
    page_object = _matrix_object(evaluative=True, role=PageObjectRole.RESPONDENT_IDENTIFIER)
    assert is_scored_matrix(page_object) is False


def test_a_matrix_without_its_config_is_not_scored():
    assert is_scored_matrix(PageObject(object_type=PageObjectType.MATRIX)) is False


def test_an_object_that_is_not_a_matrix_is_not_scored():
    assert is_scored_matrix(PageObject(object_type=PageObjectType.QUESTION)) is False


# ---------------------------------------------------------------------------
# unanswered
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("review_required", [True, False])
def test_a_flagged_result_is_unanswered_as_flagged(review_required):
    result = _result(
        detected=_detected(A, validity=Validity.VALID),
        outcome=ReviewOutcome.FLAGGED,
        review_required=review_required,
    )
    assert unanswered(result) is Unanswered.FLAGGED


def test_a_result_awaiting_review_is_unresolved():
    """Never ruled on, or ruled on and then undone: the same NULL."""
    result = _result(detected=_detected(A, validity=Validity.VALID), review_required=True)
    assert unanswered(result) is Unanswered.UNRESOLVED


def test_an_automatic_reading_that_needed_no_review_is_answered():
    result = _result(detected=_detected(A, validity=Validity.VALID), review_required=False)
    assert unanswered(result) is None


@pytest.mark.parametrize("outcome", [ReviewOutcome.CONFIRMED, ReviewOutcome.SUBSTITUTED])
def test_a_confirmed_or_substituted_result_is_answered(outcome):
    result = _result(detected=_detected(A, validity=Validity.VALID), outcome=outcome, review_required=True)
    assert unanswered(result) is None


# ---------------------------------------------------------------------------
# score_question_result
# ---------------------------------------------------------------------------


# A, B, C with B the correct answer; A is penalised.
OPTIONS = _options(-1, 2, 0)
WRONG, RIGHT, NEUTRAL = (option.id for option in OPTIONS)


@pytest.mark.parametrize("option_id", [RIGHT, WRONG])
def test_a_flagged_question_scores_zero_whatever_its_reading(option_id):
    result = _result(
        detected=_detected(option_id, validity=Validity.VALID),
        outcome=ReviewOutcome.FLAGGED,
        review_required=True,
    )
    assert score_question_result(result, OPTIONS) == QuestionResultScore(
        score=0.0, unanswered=Unanswered.FLAGGED, detail=None
    )


def test_a_pending_question_scores_zero_even_with_a_correct_reading():
    """Whatever the status of the submission, open or closed."""
    result = _result(detected=_detected(RIGHT, validity=Validity.VALID), review_required=True)
    assert score_question_result(result, OPTIONS) == QuestionResultScore(
        score=0.0, unanswered=Unanswered.UNRESOLVED, detail=None
    )


def test_an_automatic_reading_scores_its_reading():
    result = _result(detected=_detected(RIGHT, validity=Validity.VALID))
    assert score_question_result(result, OPTIONS) == QuestionResultScore(
        score=2.0,
        unanswered=None,
        detail=QuestionScore(raw_score=2.0, score=2.0, full_answer=False),
    )


def test_a_candidate_who_did_not_answer_scores_zero_with_the_reading_kept():
    result = _result(detected=_detected(validity=Validity.NO_ANSWER))
    scored = score_question_result(result, OPTIONS)
    assert (scored.score, scored.unanswered) == (0.0, None)
    assert scored.detail == QuestionScore(raw_score=0.0, score=0.0, full_answer=False)


def test_a_confirmed_question_scores_the_extracted_reading():
    result = _result(
        detected=_detected(WRONG, validity=Validity.VALID),
        outcome=ReviewOutcome.CONFIRMED,
        review_required=True,
    )
    assert score_question_result(result, OPTIONS).score == -1.0


def test_a_substituted_question_scores_the_operators_value():
    result = _result(
        detected=_detected(WRONG, validity=Validity.VALID),
        reviewed={"selected_option_ids": [str(RIGHT)], "validity": "VALID"},
        outcome=ReviewOutcome.SUBSTITUTED,
        review_required=True,
    )
    assert score_question_result(result, OPTIONS).score == 2.0


def test_a_substitution_with_no_option_is_a_no_answer_not_a_flag():
    result = _result(
        detected=_detected(RIGHT, validity=Validity.VALID),
        reviewed={"selected_option_ids": [], "validity": "NO_ANSWER"},
        outcome=ReviewOutcome.SUBSTITUTED,
        review_required=True,
    )
    scored = score_question_result(result, OPTIONS)
    assert (scored.score, scored.unanswered) == (0.0, None)


def test_a_pending_placeholder_scores_zero_as_unresolved():
    result = _result(detected=None, review_required=True)
    assert score_question_result(result, OPTIONS).unanswered is Unanswered.UNRESOLVED


def test_a_flagged_placeholder_scores_zero_as_flagged():
    result = _result(detected=None, outcome=ReviewOutcome.FLAGGED, review_required=True)
    assert score_question_result(result, OPTIONS) == QuestionResultScore(
        score=0.0, unanswered=Unanswered.FLAGGED, detail=None
    )


def test_a_substituted_placeholder_scores_the_operators_value_as_a_result():
    result = _result(
        detected=None,
        reviewed={"selected_option_ids": [str(RIGHT)], "validity": "VALID"},
        outcome=ReviewOutcome.SUBSTITUTED,
        review_required=True,
    )
    assert score_question_result(result, OPTIONS).score == 2.0


def test_a_placeholder_neither_flagged_nor_pending_is_refused():
    """A placeholder is born review_required, so this cannot happen."""
    with pytest.raises(ValueError, match="neither flagged nor pending"):
        score_question_result(_result(detected=None), OPTIONS)


def test_a_flagged_result_that_is_not_a_question_is_refused():
    result = _result(
        detected={}, outcome=ReviewOutcome.FLAGGED, result_type=SubmissionResultType.MATRIX
    )
    with pytest.raises(ValueError, match="not a QUESTION"):
        score_question_result(result, OPTIONS)


# ---------------------------------------------------------------------------
# score_matrix_result
# ---------------------------------------------------------------------------


# Every row of ROWS_DETECTED made right on the DIAGONAL grid: worth 2.
RIGHT_ROWS = {
    "rows": [
        {"row_index": 0, "selected_cells": [{"col_index": 0, "confidence": 0.9}], "validity": "VALID"},
        {"row_index": 1, "selected_cells": [{"col_index": 1, "confidence": 0.9}], "validity": "VALID"},
    ]
}


def test_a_flagged_matrix_scores_zero_on_every_row():
    """The FLAG is the outcome of the whole object."""
    result = _matrix_result(detected=RIGHT_ROWS, outcome=ReviewOutcome.FLAGGED, review_required=True)
    assert score_matrix_result(result, 3, 3, DIAGONAL) == MatrixResultScore(
        score=0.0, unanswered=Unanswered.FLAGGED, detail=None
    )


def test_a_pending_matrix_scores_zero():
    result = _matrix_result(detected=RIGHT_ROWS, review_required=True)
    assert score_matrix_result(result, 3, 3, DIAGONAL) == MatrixResultScore(
        score=0.0, unanswered=Unanswered.UNRESOLVED, detail=None
    )


def test_an_automatic_matrix_reading_scores_its_rows():
    scored = score_matrix_result(_matrix_result(detected=RIGHT_ROWS), 3, 3, DIAGONAL)
    assert (scored.score, scored.unanswered) == (2.0, None)
    assert [group.score for group in scored.detail.groups] == [1.0, 1.0]


def test_a_substitution_keeps_a_legible_row_and_leaves_the_others_empty():
    """How the operator saves the rows a FLAG would zero."""
    result = _matrix_result(
        detected=ROWS_DETECTED,
        reviewed={
            "rows": [
                {"row_index": 0, "selected_col_indices": [0], "validity": "VALID"},
                {"row_index": 1, "selected_col_indices": [], "validity": "NO_ANSWER"},
                {"row_index": 2, "selected_col_indices": [], "validity": "NO_ANSWER"},
            ]
        },
        outcome=ReviewOutcome.SUBSTITUTED,
        review_required=True,
    )
    scored = score_matrix_result(result, 3, 3, DIAGONAL)
    assert (scored.score, scored.unanswered) == (1.0, None)
    assert [group.score for group in scored.detail.groups] == [1.0, 0.0, 0.0]


def test_a_flagged_matrix_placeholder_scores_zero():
    result = _matrix_result(detected=None, outcome=ReviewOutcome.FLAGGED, review_required=True)
    assert score_matrix_result(result, 3, 3, DIAGONAL).unanswered is Unanswered.FLAGGED


def test_a_pending_matrix_placeholder_scores_zero_as_unresolved():
    result = _matrix_result(detected=None, review_required=True)
    assert score_matrix_result(result, 3, 3, DIAGONAL) == MatrixResultScore(
        score=0.0, unanswered=Unanswered.UNRESOLVED, detail=None
    )


def test_a_substituted_matrix_placeholder_scores_the_operators_rows():
    result = _matrix_result(
        detected=None,
        reviewed={
            "rows": [
                {"row_index": 0, "selected_col_indices": [0], "validity": "VALID"},
                {"row_index": 1, "selected_col_indices": [1], "validity": "VALID"},
                {"row_index": 2, "selected_col_indices": [], "validity": "NO_ANSWER"},
            ]
        },
        outcome=ReviewOutcome.SUBSTITUTED,
        review_required=True,
    )
    scored = score_matrix_result(result, 3, 3, DIAGONAL)
    assert (scored.score, scored.unanswered) == (2.0, None)


def test_a_matrix_placeholder_neither_flagged_nor_pending_is_refused():
    with pytest.raises(ValueError, match="neither flagged nor pending"):
        score_matrix_result(_matrix_result(detected=None), 3, 3, DIAGONAL)


def test_a_pending_result_that_is_not_a_matrix_is_refused():
    result = _result(detected={}, review_required=True)
    with pytest.raises(ValueError, match="not a MATRIX"):
        score_matrix_result(result, 3, 3, DIAGONAL)


# ---------------------------------------------------------------------------
# Maximum score
# ---------------------------------------------------------------------------


def _best_by_brute_force(scores: list[float | None], single: bool) -> float:
    """The best score over EVERY selection, each judged as extraction would."""
    question_type = QuestionType.SINGLE_CHOICE if single else QuestionType.MULTIPLE_CHOICE
    keyed = dict(enumerate(scores))
    return max(
        _score_selection(
            keyed, selection, compute_validity(question_type, len(selection))
        ).score
        for size in range(len(scores) + 1)
        for selection in itertools.combinations(keyed, size)
    )


_SCORE_SETS = [
    [],
    [None],
    [2.0],
    [-1.0],
    [0.0, 0.0],
    [1.0, 0.0, -1.0, 0.0],
    [1.0, 1.0],
    [1.0, 2.0, 3.0],
    [2.0, 0.5, 0.5, 1.0],
    [-1.0, -0.5, -2.0],
    [None, None, None],
    [1.0, None, 1.0],
    [3.0, -1.0, 2.0, -0.5, 1.0],
    [0.25, 0.25, 0.25, 0.25, 0.25],
]


@pytest.mark.parametrize("single", [True, False])
@pytest.mark.parametrize("scores", _SCORE_SETS)
def test_the_maximum_is_the_best_score_any_selection_earns(scores, single):
    # The formula and the scoring table never diverge.
    assert _max_selection(dict(enumerate(scores)), single) == pytest.approx(
        _best_by_brute_force(scores, single)
    )


def test_a_single_choice_maximum_is_its_best_option():
    options = _options(1.0, 3.0, -1.0)
    assert question_max_score(QuestionType.SINGLE_CHOICE, options) == 3.0


def test_a_single_choice_with_no_positive_option_has_maximum_zero():
    # Leaving it blank scores 0.
    options = _options(-1.0, 0.0, None)
    assert question_max_score(QuestionType.SINGLE_CHOICE, options) == 0.0


def test_a_multi_choice_maximum_is_the_sum_of_its_positive_options():
    options = _options(1.0, 2.0, -1.0, None)
    assert question_max_score(QuestionType.MULTIPLE_CHOICE, options) == 3.0


def test_a_multi_choice_with_every_option_positive_leaves_out_the_cheapest():
    # Marking all three is FULL_ANSWER, worth 0.
    options = _options(1.0, 2.0, 0.5)
    assert question_max_score(QuestionType.MULTIPLE_CHOICE, options) == 3.0


def test_the_only_option_of_a_multi_choice_counts_in_full():
    options = _options(2.0)
    assert question_max_score(QuestionType.MULTIPLE_CHOICE, options) == 2.0


def test_a_correct_option_without_a_score_adds_nothing_to_the_maximum():
    options = _options(None, None)
    options[0].is_correct = True
    assert question_max_score(QuestionType.SINGLE_CHOICE, options) == 0.0


def test_a_question_without_options_has_maximum_zero():
    assert question_max_score(QuestionType.MULTIPLE_CHOICE, []) == 0.0


def test_a_row_rule_matrix_adds_up_the_best_cell_of_each_row():
    cells = _cells({(0, 1): 2.0, (0, 2): 1.0, (1, 0): -1.0, (2, 2): 0.5})
    assert matrix_max_score(MatrixSelectionRule.ONE_PER_ROW, 3, 3, cells) == 2.5


def test_a_column_rule_matrix_adds_up_the_best_cell_of_each_column():
    # Rows 0 and 1 of column 0 both scored: only one may be marked.
    cells = _cells({(0, 0): 2.0, (1, 0): 1.0, (1, 1): 3.0})
    assert matrix_max_score(MatrixSelectionRule.ONE_PER_COLUMN, 2, 2, cells) == 5.0


def test_a_many_per_row_matrix_applies_full_answer_per_row():
    # Row 0: every printed bubble positive → leave out the cheapest (1).
    # Row 1: one bubble has no cell → all positives can be marked.
    cells = _cells({(0, 0): 1.0, (0, 1): 2.0, (1, 0): 1.0, (1, 1): 2.0})
    cells_row1_missing = [c for c in cells if (c.row_index, c.col_index) != (1, 1)]
    assert matrix_max_score(MatrixSelectionRule.MANY_PER_ROW, 2, 2, cells) == 4.0
    assert (
        matrix_max_score(MatrixSelectionRule.MANY_PER_ROW, 2, 2, cells_row1_missing)
        == 3.0
    )
    # A missing cell is still a printed bubble: FULL_ANSWER counts it.
    assert (
        matrix_max_score(MatrixSelectionRule.MANY_PER_ROW, 2, 3, cells) == 6.0
    )


def test_a_many_per_column_matrix_is_oriented_by_column():
    cells = _cells({(0, 0): 1.0, (1, 0): 2.0, (0, 1): 1.0})
    # Column 0: both rows positive → 2; column 1: row 1 unscored → 1.
    assert matrix_max_score(MatrixSelectionRule.MANY_PER_COLUMN, 2, 2, cells) == 3.0


def test_a_free_matrix_is_oriented_by_row():
    cells = _cells({(0, 0): 1.0, (1, 0): 2.0})
    assert matrix_max_score(MatrixSelectionRule.FREE, 2, 2, cells) == 3.0


def test_a_cell_outside_the_grid_is_ignored():
    cells = _cells({(0, 0): 1.0, (5, 5): 10.0})
    assert matrix_max_score(MatrixSelectionRule.ONE_PER_ROW, 2, 2, cells) == 1.0


def test_a_degenerate_matrix_has_maximum_zero():
    assert matrix_max_score(MatrixSelectionRule.ONE_PER_ROW, 0, 3, []) == 0.0


def _question_object(
    *scores: float | None,
    question_type: QuestionType = QuestionType.SINGLE_CHOICE,
    group: uuid.UUID | None = None,
    linked: bool = False,
) -> PageObject:
    page_object = PageObject(
        object_type=PageObjectType.QUESTION,
        shared_group_id=group,
        shared_across_pages=linked,
    )
    page_object.question_config = QuestionObjectConfig(
        question_type=question_type, question_text="Q"
    )
    page_object.question_config.options = _options(*scores)
    return page_object


def _scored_matrix_object(
    cells: dict[tuple[int, int], float | None],
    *,
    evaluative: bool = True,
    role: PageObjectRole | None = None,
) -> PageObject:
    page_object = _matrix_object(evaluative=evaluative, role=role)
    page_object.shared_across_pages = False
    page_object.matrix_config.cells = _cells(cells)
    return page_object


def test_a_variant_adds_up_its_questions_and_scored_matrices():
    objects = [
        _question_object(1.0, 2.0),
        _question_object(1.0, 1.0, question_type=QuestionType.MULTIPLE_CHOICE),
        _scored_matrix_object({(0, 0): 1.0, (1, 1): 1.0}),
    ]
    assert variant_max_score(objects) == 2.0 + 1.0 + 2.0


def test_a_matrix_that_is_not_scored_adds_nothing():
    objects = [
        _scored_matrix_object({(0, 0): 5.0}, evaluative=False),
        _scored_matrix_object(
            {(0, 0): 5.0}, role=PageObjectRole.RESPONDENT_IDENTIFIER
        ),
    ]
    assert variant_max_score(objects) == 0.0


def test_objects_that_are_not_scored_add_nothing():
    objects = [
        PageObject(object_type=PageObjectType.CODE, shared_across_pages=False),
        PageObject(object_type=PageObjectType.FREE_TEXT_AREA, shared_across_pages=False),
        PageObject(object_type=PageObjectType.TEXT, shared_across_pages=False),
        PageObject(object_type=PageObjectType.IMAGE, shared_across_pages=False),
        PageObject(object_type=PageObjectType.QUESTION, shared_across_pages=False),
    ]
    assert variant_max_score(objects) == 0.0


def test_a_linked_group_counts_once_and_a_detached_copy_on_its_own():
    # Three copies of one group, the third detached.
    group = uuid.uuid4()
    objects = [
        _question_object(2.0, 0.0, group=group, linked=True),
        _question_object(2.0, 0.0, group=group, linked=True),
        _question_object(2.0, 0.0, group=group, linked=False),
    ]
    assert variant_max_score(objects) == 4.0


def test_the_first_linked_copy_is_the_one_counted():
    # Copies are resynced; should one lag behind, the first in page order wins.
    group = uuid.uuid4()
    objects = [
        _question_object(3.0, 0.0, group=group, linked=True),
        _question_object(1.0, 0.0, group=group, linked=True),
    ]
    assert variant_max_score(objects) == 3.0


def test_an_empty_variant_has_maximum_zero():
    assert variant_max_score([]) == 0.0


# ---------------------------------------------------------------------------
# submission_score
# ---------------------------------------------------------------------------


def _with_id(page_object: PageObject) -> PageObject:
    page_object.id = uuid.uuid4()
    return page_object


def _reading(
    page_object: PageObject,
    page_order: int,
    *,
    detected: dict | None,
    reviewed: dict | None = None,
    outcome: ReviewOutcome | None = None,
    review_required: bool = False,
    result_type: SubmissionResultType = SubmissionResultType.QUESTION,
) -> SubmissionObjectResult:
    """A result of *page_object* read on the sheet in *page_order*."""
    result = _result(
        detected=detected,
        reviewed=reviewed,
        outcome=outcome,
        result_type=result_type,
        review_required=review_required,
    )
    result.id = uuid.uuid4()
    result.page_object_id = page_object.id
    result.submission_page = SubmissionPage(source_page_order=page_order)
    return result


def _marked(page_object: PageObject, *indices: int, validity: Validity = Validity.VALID) -> dict:
    options = page_object.question_config.options
    return _detected(*(options[index].id for index in indices), validity=validity)


def test_a_submission_adds_up_its_questions_and_scored_matrices():
    question = _with_id(_question_object(2.0, 0.0))
    matrix = _with_id(_scored_matrix_object({(0, 0): 1.0, (1, 1): 1.0, (2, 2): 1.0}))
    results = [
        _reading(question, 0, detected=_marked(question, 0)),
        _reading(matrix, 1, detected=RIGHT_ROWS, result_type=SubmissionResultType.MATRIX),
    ]
    assert submission_score([question, matrix], results) == SubmissionScore(
        raw_score=4.0, max_possible_score=5.0, percentage=80.0
    )


def test_the_first_duplicate_reading_counts_even_flagged_or_pending():
    """The first in physical page order, whatever the others."""
    question = _with_id(_question_object(2.0, 0.0))
    results = [
        # Listed out of order: the order is the rule's, not the caller's.
        _reading(question, 3, detected=_marked(question, 0)),
        _reading(question, 1, detected=_marked(question, 0), review_required=True),
    ]
    assert submission_score([question], results).raw_score == 0.0


def test_the_first_duplicate_reading_counts_when_it_is_the_good_one():
    question = _with_id(_question_object(2.0, 0.0))
    results = [
        _reading(question, 0, detected=_marked(question, 0)),
        _reading(question, 1, detected=_marked(question, 1)),
    ]
    assert submission_score([question], results).raw_score == 2.0


def test_a_linked_group_scores_its_first_reading_once():
    # Two linked copies are one question, the detached one is not.
    group = uuid.uuid4()
    first = _with_id(_question_object(2.0, 0.0, group=group, linked=True))
    second = _with_id(_question_object(2.0, 0.0, group=group, linked=True))
    detached = _with_id(_question_object(2.0, 0.0, group=group, linked=False))
    results = [
        _reading(first, 0, detected=_marked(first, 1)),
        _reading(second, 1, detected=_marked(second, 0)),
        _reading(detached, 2, detected=_marked(detached, 0)),
    ]
    assert submission_score([first, second, detached], results) == SubmissionScore(
        raw_score=2.0, max_possible_score=4.0, percentage=50.0
    )


def test_a_linked_matrix_scores_its_first_reading_once():
    group = uuid.uuid4()
    first = _with_id(_scored_matrix_object({(0, 0): 1.0, (1, 1): 1.0}))
    second = _with_id(_scored_matrix_object({(0, 0): 1.0, (1, 1): 1.0}))
    for copy in (first, second):
        copy.shared_group_id, copy.shared_across_pages = group, True
    wrong_rows = {
        "rows": [
            {"row_index": 0, "selected_cells": [{"col_index": 2, "confidence": 0.9}], "validity": "VALID"},
            {"row_index": 1, "selected_cells": [], "validity": "NO_ANSWER"},
        ]
    }
    results = [
        _reading(second, 1, detected=RIGHT_ROWS, result_type=SubmissionResultType.MATRIX),
        _reading(first, 0, detected=wrong_rows, result_type=SubmissionResultType.MATRIX),
    ]
    assert submission_score([first, second], results) == SubmissionScore(
        raw_score=0.0, max_possible_score=2.0, percentage=0.0
    )


def test_a_result_of_another_type_than_its_object_is_refused():
    """A MATRIX reading of a QUESTION: a broken invariant, surfaced."""
    question = _with_id(_question_object(2.0, 0.0))
    results = [
        _reading(question, 0, detected=RIGHT_ROWS, result_type=SubmissionResultType.MATRIX)
    ]
    with pytest.raises(ValueError, match="not a QUESTION"):
        submission_score([question], results)


def test_a_flagged_matrix_that_is_not_scored_stays_out_of_the_total():
    """Filtered before scoring, so it reaches neither the total nor the
    maximum."""
    question = _with_id(_question_object(2.0, 0.0))
    matrix = _with_id(_scored_matrix_object({(0, 0): 5.0}, evaluative=False))
    results = [
        _reading(question, 0, detected=_marked(question, 0)),
        _reading(
            matrix,
            0,
            detected=None,
            outcome=ReviewOutcome.FLAGGED,
            review_required=True,
            result_type=SubmissionResultType.MATRIX,
        ),
    ]
    assert submission_score([question, matrix], results) == SubmissionScore(
        raw_score=2.0, max_possible_score=2.0, percentage=100.0
    )


def test_an_object_with_no_reading_is_worth_zero_and_keeps_the_maximum():
    """Its page is missing."""
    read = _with_id(_question_object(2.0, 0.0))
    missing = _with_id(_question_object(2.0, 0.0))
    results = [_reading(read, 0, detected=_marked(read, 0))]
    assert submission_score([read, missing], results) == SubmissionScore(
        raw_score=2.0, max_possible_score=4.0, percentage=50.0
    )


def test_a_negative_total_keeps_its_negative_percentage():
    """Not clipped."""
    question = _with_id(_question_object(2.0, -1.0))
    results = [_reading(question, 0, detected=_marked(question, 1))]
    assert submission_score([question], results) == SubmissionScore(
        raw_score=-1.0, max_possible_score=2.0, percentage=-50.0
    )


def test_a_maximum_of_zero_has_no_percentage():
    question = _with_id(_question_object(0.0, -1.0))
    results = [_reading(question, 0, detected=_marked(question, 0))]
    assert submission_score([question], results) == SubmissionScore(
        raw_score=0.0, max_possible_score=0.0, percentage=None
    )


def test_a_question_without_its_config_counts_nowhere():
    """Worth 0 in the total as in the maximum."""
    bare = _with_id(PageObject(object_type=PageObjectType.QUESTION, shared_across_pages=False))
    results = [_reading(bare, 0, detected=_detected(A, validity=Validity.VALID))]
    assert submission_score([bare], results) == SubmissionScore(
        raw_score=0.0, max_possible_score=0.0, percentage=None
    )


def test_a_result_whose_object_is_not_of_the_variant_is_ignored():
    question = _with_id(_question_object(2.0, 0.0))
    stray = _with_id(_question_object(5.0, 0.0))
    results = [_reading(stray, 0, detected=_marked(stray, 0))]
    assert submission_score([question], results).raw_score == 0.0


def test_a_submission_with_no_results_scores_zero():
    question = _with_id(_question_object(2.0, 0.0))
    assert submission_score([question], []) == SubmissionScore(
        raw_score=0.0, max_possible_score=2.0, percentage=0.0
    )


def test_a_broken_reading_is_refused():
    """A selected id the question does not have: surfaced, not scored."""
    question = _with_id(_question_object(2.0, 0.0))
    results = [_reading(question, 0, detected=_detected(A, validity=Validity.VALID))]
    with pytest.raises(ValueError):
        submission_score([question], results)


# ---------------------------------------------------------------------------
# first_readings (shared by submission_score and the export)
# ---------------------------------------------------------------------------


def test_the_reading_key_is_the_group_of_a_linked_copy_only():
    group = uuid.uuid4()
    linked = _with_id(_question_object(1.0, group=group, linked=True))
    detached = _with_id(_question_object(1.0, group=group, linked=False))
    plain = _with_id(_question_object(1.0))

    assert reading_key(linked) == group
    assert reading_key(detached) == detached.id
    assert reading_key(plain) == plain.id


def test_first_readings_keeps_the_first_in_page_order_per_object():
    group = uuid.uuid4()
    first = _with_id(_question_object(1.0, 0.0, group=group, linked=True))
    second = _with_id(_question_object(1.0, 0.0, group=group, linked=True))
    other = _with_id(_question_object(1.0, 0.0))
    stray = _with_id(_question_object(1.0, 0.0))
    on_second = _reading(second, 1, detected=_marked(second, 0))
    on_first = _reading(first, 2, detected=_marked(first, 1))
    duplicate_late = _reading(other, 5, detected=_marked(other, 0))
    duplicate_early = _reading(other, 0, detected=_marked(other, 1))
    ignored = _reading(stray, 0, detected=_marked(stray, 0))

    readings = first_readings(
        {o.id: o for o in (first, second, other)},
        [on_first, on_second, duplicate_late, duplicate_early, ignored],
    )

    assert readings == {
        group: (on_second, second),
        other.id: (duplicate_early, other),
    }
