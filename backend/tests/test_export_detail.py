# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure unit tests for ``export_detail``: the columns per question of the
Detail export.

Positions in reading order and their families, the answers, the "no answer"
symbol, the points, texts and codes, the legend, and a broken result.

No DB: transient ORM objects, as in ``test_scoring_rules``.
"""
from __future__ import annotations

import logging
import uuid

import pytest

from app.models.enums import (
    MatrixSelectionRule,
    PageObjectRole,
    PageObjectType,
    QuestionType,
    ReviewOutcome,
    SubmissionResultType,
    Validity,
)
from app.models.form_variant_page import FormVariantPage
from app.models.form_variant_role_binding import FormVariantRoleBinding
from app.models.free_text_area_config import FreeTextAreaConfig
from app.models.matrix_cell_def import MatrixCellDef
from app.models.matrix_col_def import MatrixColDef
from app.models.matrix_object_config import MatrixObjectConfig
from app.models.matrix_row_def import MatrixRowDef
from app.models.page_object import PageObject
from app.models.question_object_config import QuestionObjectConfig
from app.models.question_option import QuestionOption
from app.models.submission_object_result import SubmissionObjectResult
from app.models.submission_page import SubmissionPage
from app.services.export_detail import (
    NO_ANSWER,
    Family,
    Layout,
    describe,
    submission_cells,
    legend_column,
    option_letter,
    variant_positions,
)

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _place(page_object: PageObject, page: int, y: float, x: float = 10.0) -> PageObject:
    page_object.id = uuid.uuid4()
    page_object.form_variant_page = FormVariantPage(page_index=page)
    page_object.y = y
    page_object.x = x
    page_object.shared_across_pages = False
    return page_object


def _question(
    *scores: float | None,
    page: int = 1,
    y: float = 10.0,
    x: float = 10.0,
    text: str = "Q",
    question_type: QuestionType = QuestionType.SINGLE_CHOICE,
) -> PageObject:
    page_object = PageObject(object_type=PageObjectType.QUESTION)
    page_object.question_config = QuestionObjectConfig(
        question_type=question_type, question_text=text
    )
    page_object.question_config.options = [
        QuestionOption(id=uuid.uuid4(), order_index=index, option_text=f"o{index}", score=score)
        for index, score in enumerate(scores)
    ]
    return _place(page_object, page, y, x)


def _matrix(
    rows: int,
    cols: int,
    *,
    evaluative: bool = True,
    rule: MatrixSelectionRule = MatrixSelectionRule.ONE_PER_ROW,
    cells: dict[tuple[int, int], float] | None = None,
    page: int = 1,
    y: float = 10.0,
    label: str | None = "Grid",
    row_labels: list[str] | None = None,
    col_labels: list[str] | None = None,
) -> PageObject:
    page_object = PageObject(object_type=PageObjectType.MATRIX, label=label)
    config = MatrixObjectConfig(
        rows_count=rows, cols_count=cols, evaluative=evaluative, selection_rule=rule
    )
    config.cells = [
        MatrixCellDef(row_index=r, col_index=c, score=score)
        for (r, c), score in (cells or {}).items()
    ]
    config.rows = [
        MatrixRowDef(order_index=i, label=label)
        for i, label in enumerate(row_labels or [f"R{i + 1}" for i in range(rows)])
    ]
    config.cols = [
        MatrixColDef(order_index=i, label=label)
        for i, label in enumerate(col_labels or [f"C{i + 1}" for i in range(cols)])
    ]
    page_object.matrix_config = config
    return _place(page_object, page, y)


def _free_text(*, page: int = 1, y: float = 10.0, label: str | None = None) -> PageObject:
    page_object = PageObject(object_type=PageObjectType.FREE_TEXT_AREA, label=label)
    page_object.free_text_config = FreeTextAreaConfig(placeholder="Your comment")
    return _place(page_object, page, y)


def _code(*, page: int = 1, y: float = 10.0, role: PageObjectRole | None = None) -> PageObject:
    page_object = PageObject(object_type=PageObjectType.CODE)
    if role is not None:
        page_object.role_binding = FormVariantRoleBinding(role=role)
    return _place(page_object, page, y)


def _result(
    page_object: PageObject,
    result_type: SubmissionResultType,
    *,
    detected: dict | None,
    reviewed: dict | None = None,
    outcome: ReviewOutcome | None = None,
    review_required: bool = False,
    sheet: int = 0,
) -> SubmissionObjectResult:
    result = SubmissionObjectResult(
        id=uuid.uuid4(),
        page_object_id=page_object.id,
        result_type=result_type,
        detected_payload_json=detected,
        reviewed_payload_json=reviewed,
        review_outcome=outcome,
        review_required=review_required,
        validity_forces_review=False,
    )
    result.submission_page = SubmissionPage(source_page_order=sheet)
    return result


def _answered(question: PageObject, *indices: int, validity: Validity = Validity.VALID, **kwargs):
    options = question.question_config.options
    return _result(
        question,
        SubmissionResultType.QUESTION,
        detected={
            "selected_options": [
                {"option_id": str(options[i].id), "confidence": 0.95} for i in indices
            ],
            "validity": validity.value,
        },
        **kwargs,
    )


def _matrix_rows(matrix: PageObject, marks: dict[int, set[int]], **kwargs):
    rows = []
    for row in range(matrix.matrix_config.rows_count):
        selected = marks.get(row, set())
        validity = (
            Validity.NO_ANSWER
            if not selected
            else Validity.VALID
            if len(selected) == 1
            else Validity.TOO_MANY_ANSWERS
        )
        rows.append(
            {
                "row_index": row,
                "selected_cells": [{"col_index": c, "confidence": 0.9} for c in sorted(selected)],
                "validity": validity.value,
            }
        )
    return _result(matrix, SubmissionResultType.MATRIX, detected={"rows": rows}, **kwargs)


def _cells(objects: list[PageObject], results: list[SubmissionObjectResult]):
    positions = variant_positions(objects)
    layout = Layout.of([positions])
    return layout, submission_cells(layout, positions, objects, results)


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------


def test_positions_follow_the_reading_order():
    lower = _question(1, 0, page=1, y=200.0, text="lower")
    right = _question(1, 0, page=1, y=50.0, x=300.0, text="right")
    left = _question(1, 0, page=1, y=50.0, x=10.0, text="left")
    next_page = _question(1, 0, page=2, y=5.0, text="next page")

    positions = variant_positions([next_page, lower, right, left])

    assert [p.page_object.question_config.question_text for p in positions] == [
        "left",
        "right",
        "lower",
        "next page",
    ]


def test_a_linked_object_is_one_position_at_its_first_copy():
    group = uuid.uuid4()
    first = _question(1, 0, page=1, y=10.0)
    again = _question(1, 0, page=2, y=10.0)
    detached = _question(1, 0, page=3, y=10.0)
    for copy in (first, again, detached):
        copy.shared_group_id = group
    first.shared_across_pages = again.shared_across_pages = True

    positions = variant_positions([again, detached, first])

    assert [p.page_object for p in positions] == [first, detached]


def test_matrix_groups_are_consecutive_positions_oriented_by_the_rule():
    by_row = _matrix(3, 2, y=10.0)
    by_column = _matrix(3, 2, rule=MatrixSelectionRule.ONE_PER_COLUMN, y=20.0)

    positions = variant_positions([by_row, by_column])

    assert [(p.page_object, p.group) for p in positions] == [
        (by_row, 0),
        (by_row, 1),
        (by_row, 2),
        (by_column, 0),
        (by_column, 1),
    ]


def test_each_family_has_its_own_numbering_and_its_place_in_the_file():
    objects = [
        _free_text(y=1.0),
        _matrix(2, 2, evaluative=False, y=2.0),
        _question(1, 0, y=3.0),
        _code(y=4.0),
        _matrix(1, 2, y=5.0),
    ]

    positions = variant_positions(objects)
    layout = Layout.of([positions])

    assert [p.family for p in positions] == [
        Family.T,
        Family.M,
        Family.M,
        Family.D,
        Family.C,
        Family.D,
    ]
    assert layout.headers() == ("D1", "D1_pt", "D2", "D2_pt", "M1", "M2", "T1", "C1")
    assert layout.names() == ("D1", "D2", "M1", "M2", "T1", "C1")


@pytest.mark.parametrize(
    "role", [PageObjectRole.RESPONDENT_IDENTIFIER, PageObjectRole.VARIANT_IDENTIFIER]
)
def test_an_object_with_a_role_has_no_column(role):
    grid = _matrix(3, 10, evaluative=True)
    grid.role_binding = FormVariantRoleBinding(role=PageObjectRole.RESPONDENT_IDENTIFIER)
    identifying_text = _free_text()
    identifying_text.role_binding = FormVariantRoleBinding(
        role=PageObjectRole.RESPONDENT_IDENTIFIER
    )

    assert variant_positions([grid, identifying_text, _code(role=role)]) == []


def test_text_image_and_unconfigured_objects_have_no_column():
    objects = [
        _place(PageObject(object_type=PageObjectType.TEXT), 1, 1.0),
        _place(PageObject(object_type=PageObjectType.IMAGE), 1, 2.0),
        _place(PageObject(object_type=PageObjectType.QUESTION), 1, 3.0),
        _place(PageObject(object_type=PageObjectType.MATRIX), 1, 4.0),
    ]
    assert variant_positions(objects) == []


def test_the_layout_takes_the_longest_variant_of_each_family():
    long_questions = variant_positions([_question(1, 0, y=1.0), _question(1, 0, y=2.0)])
    with_text = variant_positions([_question(1, 0), _free_text(y=5.0)])

    layout = Layout.of([long_questions, with_text])

    assert layout.headers() == ("D1", "D1_pt", "D2", "D2_pt", "T1")


def test_a_shorter_variant_leaves_the_columns_beyond_it_empty():
    question = _question(2, 0)
    positions = variant_positions([question])
    layout = Layout.of([positions, variant_positions([_question(1, 0), _question(1, 0, y=50)])])

    cells = submission_cells(layout, positions, [question], [_answered(question, 0)])

    assert cells == ("A", 2.0, None, None)


# ---------------------------------------------------------------------------
# Answers and points
# ---------------------------------------------------------------------------


def test_option_letters_follow_the_review():
    assert [option_letter(i) for i in (0, 1, 25, 26, 27, 51, 52)] == [
        "A", "B", "Z", "AA", "AB", "AZ", "BA",
    ]


def test_a_valid_answer_has_its_letter_and_its_points():
    question = _question(0, 3, -1)
    assert _cells([question], [_answered(question, 1)])[1] == ("B", 3.0)


def test_several_letters_are_joined_in_option_order():
    question = _question(1, 1, 1, 0, question_type=QuestionType.MULTIPLE_CHOICE)
    assert _cells([question], [_answered(question, 2, 0)])[1] == ("A, C", 2.0)


def test_options_are_lettered_by_their_order_not_their_list_position():
    question = _question(1, 0)
    first, second = question.question_config.options
    first.order_index, second.order_index = 1, 0

    assert _cells([question], [_answered(question, 0)])[1] == ("B", 1.0)


def test_too_many_answers_show_the_letters_and_never_score_positive():
    question = _question(2, -1, 0)
    positive = _answered(question, 0, 2, validity=Validity.TOO_MANY_ANSWERS)
    negative = _answered(question, 1, 2, validity=Validity.TOO_MANY_ANSWERS)
    assert _cells([question], [positive])[1] == ("A, C", 0.0)
    assert _cells([question], [negative])[1] == ("B, C", -1.0)


def test_a_full_answer_shows_the_letters_and_scores_zero():
    question = _question(1, 1, question_type=QuestionType.MULTIPLE_CHOICE)
    assert _cells([question], [_answered(question, 0, 1)])[1] == ("A, B", 0.0)


def test_the_candidate_not_answering_is_the_symbol_and_zero():
    question = _question(2, 0)
    result = _answered(question, validity=Validity.NO_ANSWER)
    assert _cells([question], [result])[1] == (NO_ANSWER, 0.0)


def test_a_flag_is_the_symbol_and_zero():
    question = _question(2, 0)
    result = _answered(question, 0, outcome=ReviewOutcome.FLAGGED, review_required=True)
    assert _cells([question], [result])[1] == (NO_ANSWER, 0.0)


def test_a_pending_object_is_the_symbol_and_zero():
    question = _question(2, 0)
    result = _answered(question, 0, review_required=True)
    assert _cells([question], [result])[1] == (NO_ANSWER, 0.0)


def test_a_substituted_answer_is_the_one_exported():
    question = _question(2, 5)
    options = question.question_config.options
    result = _answered(
        question,
        0,
        outcome=ReviewOutcome.SUBSTITUTED,
        review_required=True,
        reviewed={"selected_option_ids": [str(options[1].id)], "validity": "VALID"},
    )
    assert _cells([question], [result])[1] == ("B", 5.0)


def test_an_object_without_a_reading_is_the_symbol_and_zero():
    """Its page is missing: "no answer", worth 0 as in the score."""
    question = _question(2, 0)
    text = _free_text(y=50.0)
    matrix = _matrix(2, 2, y=90.0)
    assert _cells([question, text, matrix], [])[1] == (
        NO_ANSWER, 0.0, NO_ANSWER, 0.0, NO_ANSWER, 0.0, NO_ANSWER,
    )


def test_the_first_duplicate_reading_is_exported_even_when_flagged():
    question = _question(2, 0)
    flagged = _answered(question, 0, outcome=ReviewOutcome.FLAGGED, review_required=True, sheet=1)
    later = _answered(question, 0, sheet=4)
    assert _cells([question], [later, flagged])[1] == (NO_ANSWER, 0.0)


def test_a_linked_object_exports_its_first_reading_once():
    group = uuid.uuid4()
    first = _question(2, 0, page=1)
    again = _question(2, 0, page=2)
    for copy in (first, again):
        copy.shared_group_id = group
        copy.shared_across_pages = True
    read_on_page_two = _answered(again, 0, sheet=1)
    read_on_page_one = _answered(first, 1, sheet=0)

    layout, cells = _cells([first, again], [read_on_page_two, read_on_page_one])

    assert layout.headers() == ("D1", "D1_pt")
    assert cells == ("B", 0.0)


def _linked_pair() -> tuple[PageObject, PageObject]:
    """One question shown on pages 1 and 2: two copies, each with options of
    its own (other ids), as authoring clones them."""
    group = uuid.uuid4()
    first = _question(0, 2, page=1)
    again = _question(0, 2, page=2)
    for copy in (first, again):
        copy.shared_group_id = group
        copy.shared_across_pages = True
    return first, again


def test_a_linked_object_read_only_on_a_later_copy_is_exported():
    """Page 1 missing, the reading is on the copy of page 2
    and must be read against that copy's options, as the score does."""
    from app.services import scoring_rules

    first, again = _linked_pair()
    results = [_answered(again, 1, sheet=0)]

    cells = _cells([first, again], results)[1]

    assert cells == ("B", 2.0)
    assert scoring_rules.submission_score([first, again], results).raw_score == 2.0


def test_a_linked_object_whose_later_copy_comes_first_on_paper_is_exported():
    """The sheet of page 2 physically before the one of page 1: its reading
    is the first, and the column still sits at the first copy."""
    first, again = _linked_pair()
    results = [_answered(first, 0, sheet=1), _answered(again, 1, sheet=0)]

    layout, cells = _cells([first, again], results)

    assert layout.headers() == ("D1", "D1_pt")
    assert cells == ("B", 2.0)


def test_no_answer_validity_with_marks_is_still_the_symbol():
    """A NO_ANSWER verdict wins over the marks listed: the symbol, and
    the points of the scoring (0)."""
    question = _question(2, 0)
    result = _answered(question, 0, validity=Validity.NO_ANSWER)
    assert _cells([question], [result])[1] == (NO_ANSWER, 0.0)


def test_matrix_rows_have_their_labels_and_points():
    matrix = _matrix(
        3,
        3,
        cells={(0, 0): 1.0, (1, 1): 2.0, (2, 2): 1.0},
        col_labels=["Agree", "Neutral", "Disagree"],
    )
    result = _matrix_rows(matrix, {0: {0}, 1: {0, 2}, 2: set()})

    assert _cells([matrix], [result])[1] == (
        "Agree", 1.0,
        "Agree, Disagree", 0.0,
        NO_ANSWER, 0.0,
    )


def test_a_matrix_group_without_a_label_shows_its_number():
    matrix = _matrix(1, 3, cells={(0, 1): 1.0}, col_labels=["a", "", "c"])
    assert _cells([matrix], [_matrix_rows(matrix, {0: {1}})])[1] == ("2", 1.0)


def test_a_column_rule_exports_the_columns_with_the_row_labels():
    matrix = _matrix(
        2,
        2,
        rule=MatrixSelectionRule.ONE_PER_COLUMN,
        cells={(1, 0): 1.0, (0, 1): 1.0},
        row_labels=["yes", "no"],
    )
    result = _result(
        matrix,
        SubmissionResultType.MATRIX,
        detected={
            "cols": [
                {"col_index": 0, "selected_cells": [{"row_index": 1, "confidence": 0.9}], "validity": "VALID"},
                {"col_index": 1, "selected_cells": [{"row_index": 1, "confidence": 0.9}], "validity": "VALID"},
            ]
        },
    )
    assert _cells([matrix], [result])[1] == ("no", 1.0, "no", 0.0)


def test_a_flag_on_a_matrix_zeroes_every_row():
    matrix = _matrix(2, 2, cells={(0, 0): 1.0, (1, 1): 1.0})
    result = _matrix_rows(
        matrix, {0: {0}, 1: {1}}, outcome=ReviewOutcome.FLAGGED, review_required=True
    )
    assert _cells([matrix], [result])[1] == (NO_ANSWER, 0.0, NO_ANSWER, 0.0)


def test_a_matrix_that_is_not_evaluative_has_answers_and_no_points():
    matrix = _matrix(2, 2, evaluative=False, cells={(0, 0): 5.0})
    layout, cells = _cells([matrix], [_matrix_rows(matrix, {0: {0}})])

    assert layout.headers() == ("M1", "M2")
    assert cells == ("C1", NO_ANSWER)


def test_the_points_add_up_to_the_submission_score():
    from app.services import scoring_rules

    question = _question(2, 0, -1, y=1.0)
    other = _question(1, 1, question_type=QuestionType.MULTIPLE_CHOICE, y=2.0)
    matrix = _matrix(2, 2, cells={(0, 0): 1.5, (1, 0): -0.5, (1, 1): 1.0}, y=3.0)
    results = [
        _answered(question, 2),
        _answered(other, 0, 1),
        _matrix_rows(matrix, {0: {0}, 1: {0}}),
    ]
    objects = [question, other, matrix]

    layout, cells = _cells(objects, results)

    points = [cells[i] for i, header in enumerate(layout.headers()) if header.endswith("_pt")]
    assert sum(points) == scoring_rules.submission_score(objects, results).raw_score


# ---------------------------------------------------------------------------
# Texts and codes
# ---------------------------------------------------------------------------


def _text_result(text_object, suggested="ocr text", **kwargs):
    return _result(
        text_object,
        SubmissionResultType.FREE_TEXT,
        detected={"suggested_text": suggested},
        review_required=True,
        **kwargs,
    )


def test_a_confirmed_text_is_exported():
    text = _free_text()
    result = _text_result(text, outcome=ReviewOutcome.CONFIRMED)
    assert _cells([text], [result])[1] == ("ocr text",)


def test_a_corrected_text_is_exported():
    text = _free_text()
    result = _text_result(text, outcome=ReviewOutcome.SUBSTITUTED, reviewed={"text": "=fixed"})
    assert _cells([text], [result])[1] == ("=fixed",)


@pytest.mark.parametrize(
    ("outcome", "reviewed"),
    [
        (None, None),  # pending: the OCR text is only a suggestion
        (ReviewOutcome.FLAGGED, None),
        (ReviewOutcome.SUBSTITUTED, {"text": "  "}),  # "nothing written"
    ],
)
def test_an_unverified_or_empty_text_is_the_symbol(outcome, reviewed):
    text = _free_text()
    result = _text_result(text, outcome=outcome, reviewed=reviewed)
    assert _cells([text], [result])[1] == (NO_ANSWER,)


def test_a_decoded_code_is_exported():
    code = _code()
    result = _result(
        code,
        SubmissionResultType.CODE,
        detected={"decoded_value": "ABC-1", "decode_successful": True},
    )
    assert _cells([code], [result])[1] == ("ABC-1",)


def test_a_corrected_code_is_exported():
    code = _code()
    result = _result(
        code,
        SubmissionResultType.CODE,
        detected={"decoded_value": None, "decode_successful": False},
        reviewed={"value": "XYZ"},
        outcome=ReviewOutcome.SUBSTITUTED,
        review_required=True,
    )
    assert _cells([code], [result])[1] == ("XYZ",)


@pytest.mark.parametrize("outcome", [None, ReviewOutcome.FLAGGED])
def test_an_undecoded_code_not_corrected_is_the_symbol(outcome):
    code = _code()
    result = _result(
        code,
        SubmissionResultType.CODE,
        detected={"decoded_value": None, "decode_successful": False},
        outcome=outcome,
        review_required=True,
    )
    assert _cells([code], [result])[1] == (NO_ANSWER,)


# ---------------------------------------------------------------------------
# Broken result
# ---------------------------------------------------------------------------


def test_a_broken_result_leaves_only_its_cells_empty(caplog):
    broken = _question(2, 0, y=1.0)
    fine = _question(1, 0, y=2.0)
    unknown_option = _result(
        broken,
        SubmissionResultType.QUESTION,
        detected={
            "selected_options": [{"option_id": str(uuid.uuid4()), "confidence": 0.9}],
            "validity": "VALID",
        },
    )

    with caplog.at_level(logging.ERROR):
        cells = _cells([broken, fine], [unknown_option, _answered(fine, 0)])[1]

    assert cells == (None, None, "A", 1.0)
    assert "Cannot export result" in caplog.text


# ---------------------------------------------------------------------------
# Legend
# ---------------------------------------------------------------------------


def test_the_legend_says_what_each_column_is_in_a_variant():
    question = _question(1, 0, page=2, text="Capital of France?")
    matrix = _matrix(1, 2, evaluative=False, label="Survey", row_labels=["Pace"], page=2, y=50)
    text = _free_text(page=3, label=None)
    a = variant_positions([question, matrix, text])
    b = variant_positions([_question(1, 0), _question(1, 0, y=40.0)])
    layout = Layout.of([a, b])

    assert layout.names() == ("D1", "D2", "M1", "T1")
    assert legend_column(layout, a) == (
        "p. 2 · Capital of France?",
        None,
        "p. 2 · Survey · Pace",
        "p. 3 · Your comment",
    )
    assert legend_column(layout, b)[2:] == (None, None)


def test_a_code_is_described_by_its_label():
    code = _code()
    code.label = "Lab bench"
    [position] = variant_positions([code])
    assert describe(position) == "p. 1 · Lab bench"
