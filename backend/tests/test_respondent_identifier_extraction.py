# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure (DB-free) tests for RESPONDENT_IDENTIFIER extraction.

Everything here is arithmetic and dict logic — the per-column resolution and
the aggregation across the whole grid — exercised directly on hand-built
``selected`` dicts, the same style ``test_matrix_extraction.py`` uses for
``_build_rows``/``_build_cols``. No Postgres, no session, no image: the
DB-backed half (real synthetic scans, preconditions, the degenerate grid,
threshold integration) lives in
``tests/integration/test_respondent_identifier_extraction_integration.py``.
"""
from __future__ import annotations

from app.services import respondent_identifier_extraction_service as svc


# ---------------------------------------------------------------------------
# _resolve_column
# ---------------------------------------------------------------------------
def test_a_column_with_no_selection_reads_as_a_question_mark():
    char, digit, ambiguous = svc._resolve_column(2, {}, rows_count=10, row_labels={})

    assert char == "?"
    assert digit is None
    assert ambiguous is False


def test_a_single_selection_reads_its_label():
    selected = {(3, 2): 0.9}

    char, digit, ambiguous = svc._resolve_column(
        2, selected, rows_count=10, row_labels={3: "3"}
    )

    assert char == "3"
    assert digit.col_index == 2
    assert digit.row_index == 3
    assert digit.label == "3"
    assert digit.confidence == 0.9
    assert ambiguous is False


def test_a_row_without_an_explicit_label_falls_back_to_the_index():
    """Same tolerance ``_render_matrix``'s ``row_labels.get(rr, str(rr))``
    already applies: a row with no ``MatrixRowDef`` still has a truthful
    string to report."""
    selected = {(7, 0): 0.8}

    char, digit, _ = svc._resolve_column(0, selected, rows_count=10, row_labels={})

    assert char == "7"
    assert digit.label == "7"


def test_several_selections_pick_the_highest_confidence():
    selected = {(1, 0): 0.6, (4, 0): 0.95, (8, 0): 0.7}

    char, digit, ambiguous = svc._resolve_column(
        0, selected, rows_count=10, row_labels={4: "4"}
    )

    assert char == "4"
    assert digit.row_index == 4
    assert digit.confidence == 0.95
    assert ambiguous is True


def test_a_confidence_tie_is_broken_by_the_lowest_row_index():
    """The deterministic tie-break, stated explicitly: two cells of the same
    column read with the identical fill ratio must not resolve arbitrarily."""
    selected = {(5, 1): 0.9, (2, 1): 0.9, (9, 1): 0.9}

    char, digit, ambiguous = svc._resolve_column(
        1, selected, rows_count=10, row_labels={2: "2", 5: "5", 9: "9"}
    )

    assert char == "2"
    assert digit.row_index == 2
    assert ambiguous is True


# ---------------------------------------------------------------------------
# _build_digits — the whole grid
# ---------------------------------------------------------------------------
def test_every_column_is_reported_in_order():
    row_labels = {i: str(i) for i in range(10)}
    selected = {(3, 0): 0.9, (7, 2): 0.8, (1, 4): 0.99}

    raw_value, digits, ambiguous_columns, confidence_score = svc._build_digits(
        selected, rows_count=10, cols_count=5, row_labels=row_labels
    )

    assert raw_value == "3?7?1"
    assert [d.col_index for d in digits] == [0, 2, 4]
    assert ambiguous_columns == []


def test_an_empty_column_contributes_zero_to_the_mean_not_excluded():
    """The digit-missing-is-a-signal reading: unlike a QUESTION option nobody
    was forced to select, an empty column of an identifier still counts
    against the average."""
    row_labels = {i: str(i) for i in range(10)}
    selected = {(3, 0): 1.0, (5, 1): 1.0}

    raw_value, digits, ambiguous_columns, confidence_score = svc._build_digits(
        selected, rows_count=10, cols_count=4, row_labels=row_labels
    )

    assert raw_value == "35??"
    assert len(digits) == 2
    # (1.0 + 1.0 + 0.0 + 0.0) / 4, not / 2.
    assert confidence_score == 0.5


def test_an_ambiguous_column_reports_the_winner_everywhere():
    row_labels = {i: str(i) for i in range(10)}
    selected = {(0, 0): 0.5, (1, 0): 0.9}

    raw_value, digits, ambiguous_columns, confidence_score = svc._build_digits(
        selected, rows_count=10, cols_count=1, row_labels=row_labels
    )

    assert raw_value == "1"
    assert digits[0].row_index == 1
    assert digits[0].confidence == 0.9
    assert ambiguous_columns == [0]
    # The winner's confidence contributes, not a penalty for the ambiguity.
    assert confidence_score == 0.9


def test_the_mean_is_arithmetic_over_all_columns():
    row_labels = {i: str(i) for i in range(10)}
    selected = {(0, 0): 1.0, (0, 1): 0.5, (0, 2): 0.25}

    raw_value, digits, ambiguous_columns, confidence_score = svc._build_digits(
        selected, rows_count=10, cols_count=4, row_labels=row_labels
    )

    assert confidence_score == (1.0 + 0.5 + 0.25 + 0.0) / 4
    assert raw_value == "000?"
