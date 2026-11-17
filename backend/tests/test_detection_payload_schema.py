# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure (DB-free) tests for the Confidence/Validity extraction contract.

These guard the SHAPE of ``SubmissionObjectResult.detected_payload_json`` for
QUESTION and MATRIX results — see ``app.schemas.submission_result``. Nothing
here touches the DB: they are plain Pydantic models, so the suite runs anywhere
(like ``test_user_profile_schema.py``).

The most important invariant under test is a NEGATIVE one: Confidence and
Validity are independent axes, and no schema-level rule derives one from the
other.
"""
from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.models.enums import Validity
from app.schemas.submission_result import (
    MatrixCellDetection,
    MatrixColDetection,
    MatrixDetectionPayload,
    MatrixRowCellDetection,
    MatrixRowDetection,
    OptionDetection,
    QuestionDetectionPayload,
)


# ---------------------------------------------------------------------------
# QUESTION — valid construction
# ---------------------------------------------------------------------------


def test_question_payload_with_one_selected_option():
    option_id = uuid.uuid4()
    payload = QuestionDetectionPayload(
        selected_options=[OptionDetection(option_id=option_id, confidence=0.93)],
        validity=Validity.VALID,
    )

    assert len(payload.selected_options) == 1
    assert payload.selected_options[0].option_id == option_id
    assert payload.selected_options[0].confidence == pytest.approx(0.93)
    assert payload.validity is Validity.VALID


def test_question_payload_from_raw_jsonb_dict():
    # What the QUESTION extractor writes into the JSONB column.
    option_id = uuid.uuid4()
    raw = {
        "selected_options": [{"option_id": str(option_id), "confidence": 0.71}],
        "validity": "VALID",
    }
    payload = QuestionDetectionPayload.model_validate(raw)

    assert payload.selected_options[0].option_id == option_id
    assert payload.validity is Validity.VALID


def test_question_payload_round_trips_through_json_mode():
    # ``mode="json"`` is what makes the dump JSONB-compatible: UUID -> str,
    # Validity -> its plain string value. Re-validating must give back an
    # identical model, so producer and consumer agree on the wire form.
    payload = QuestionDetectionPayload(
        selected_options=[
            OptionDetection(option_id=uuid.uuid4(), confidence=0.5),
            OptionDetection(option_id=uuid.uuid4(), confidence=1.0),
        ],
        validity=Validity.TOO_MANY_ANSWERS,
    )
    dumped = payload.model_dump(mode="json")

    assert isinstance(dumped["selected_options"][0]["option_id"], str)
    assert dumped["validity"] == "TOO_MANY_ANSWERS"
    assert QuestionDetectionPayload.model_validate(dumped) == payload


def test_multiple_choice_question_may_have_several_selected_options():
    payload = QuestionDetectionPayload(
        selected_options=[
            OptionDetection(option_id=uuid.uuid4(), confidence=0.88),
            OptionDetection(option_id=uuid.uuid4(), confidence=0.62),
            OptionDetection(option_id=uuid.uuid4(), confidence=0.95),
        ],
        validity=Validity.VALID,
    )
    assert len(payload.selected_options) == 3


# ---------------------------------------------------------------------------
# Confidence bounds — [0, 1] inclusive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("confidence", [0.0, 0.5, 1.0])
def test_confidence_accepts_the_closed_unit_interval(confidence):
    assert OptionDetection(
        option_id=uuid.uuid4(), confidence=confidence
    ).confidence == pytest.approx(confidence)
    assert MatrixCellDetection(
        col_index=0, confidence=confidence
    ).confidence == pytest.approx(confidence)


@pytest.mark.parametrize("confidence", [-0.01, 1.01, 2.0, -1.0])
def test_option_confidence_outside_unit_interval_is_rejected(confidence):
    with pytest.raises(ValidationError):
        OptionDetection(option_id=uuid.uuid4(), confidence=confidence)


@pytest.mark.parametrize("confidence", [-0.01, 1.01, 42.0])
def test_matrix_cell_confidence_outside_unit_interval_is_rejected(confidence):
    with pytest.raises(ValidationError):
        MatrixCellDetection(col_index=0, confidence=confidence)


def test_out_of_range_confidence_is_rejected_through_the_whole_payload():
    # The bound is enforced when validating a raw dict too, not only on the
    # leaf model — a bad producer cannot smuggle it in nested.
    with pytest.raises(ValidationError):
        QuestionDetectionPayload.model_validate(
            {
                "selected_options": [
                    {"option_id": str(uuid.uuid4()), "confidence": 1.5}
                ],
                "validity": "VALID",
            }
        )


def test_confidence_is_required_on_a_selected_option():
    # A selected option without a confidence is not a valid reading.
    with pytest.raises(ValidationError):
        OptionDetection(option_id=uuid.uuid4())


# ---------------------------------------------------------------------------
# Validity — exactly three values
# ---------------------------------------------------------------------------


def test_validity_has_exactly_the_three_enumerated_values():
    assert {v.value for v in Validity} == {
        "VALID",
        "NO_ANSWER",
        "TOO_MANY_ANSWERS",
    }


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("VALID", Validity.VALID),
        ("NO_ANSWER", Validity.NO_ANSWER),
        ("TOO_MANY_ANSWERS", Validity.TOO_MANY_ANSWERS),
    ],
)
def test_validity_accepts_each_enumerated_value(raw, expected):
    payload = QuestionDetectionPayload.model_validate(
        {"selected_options": [], "validity": raw}
    )
    assert payload.validity is expected


@pytest.mark.parametrize("raw", ["INVALID", "valid", "TOO_MANY", "", None])
def test_validity_rejects_anything_else(raw):
    with pytest.raises(ValidationError):
        QuestionDetectionPayload.model_validate(
            {"selected_options": [], "validity": raw}
        )


def test_validity_is_required():
    with pytest.raises(ValidationError):
        QuestionDetectionPayload.model_validate({"selected_options": []})


# ---------------------------------------------------------------------------
# Empty selections are legitimate — and stay independent from validity
# ---------------------------------------------------------------------------


def test_empty_selected_options_is_a_legitimate_reading():
    # Nothing blackened: no confidence entries at all (QUESTION extraction
    # computes confidence only for selected options).
    payload = QuestionDetectionPayload(
        selected_options=[], validity=Validity.NO_ANSWER
    )
    assert payload.selected_options == []
    assert payload.validity is Validity.NO_ANSWER


@pytest.mark.parametrize(
    "validity",
    [Validity.VALID, Validity.NO_ANSWER, Validity.TOO_MANY_ANSWERS],
)
def test_empty_selected_options_accepts_any_validity(validity):
    # Explicitly NOT cross-validated: the schema must not deduce
    # "empty selection => NO_ANSWER". That is extraction logic, and the two
    # axes must stay independent.
    payload = QuestionDetectionPayload(selected_options=[], validity=validity)
    assert payload.validity is validity


@pytest.mark.parametrize(
    "validity",
    [Validity.VALID, Validity.NO_ANSWER, Validity.TOO_MANY_ANSWERS],
)
def test_non_empty_selection_accepts_any_validity(validity):
    # The mirror case: a confident reading may still be not valid (two bubbles
    # blackened on a single-choice question), so no combination is forbidden.
    payload = QuestionDetectionPayload(
        selected_options=[
            OptionDetection(option_id=uuid.uuid4(), confidence=1.0),
            OptionDetection(option_id=uuid.uuid4(), confidence=0.99),
        ],
        validity=validity,
    )
    assert payload.validity is validity


def test_empty_selected_cells_is_a_legitimate_matrix_row():
    row = MatrixRowDetection(
        row_index=2, selected_cells=[], validity=Validity.NO_ANSWER
    )
    assert row.selected_cells == []


@pytest.mark.parametrize(
    "validity",
    [Validity.VALID, Validity.NO_ANSWER, Validity.TOO_MANY_ANSWERS],
)
def test_empty_selected_cells_accepts_any_validity(validity):
    row = MatrixRowDetection(row_index=0, selected_cells=[], validity=validity)
    assert row.validity is validity


# ---------------------------------------------------------------------------
# MATRIX — valid construction
# ---------------------------------------------------------------------------


def test_matrix_payload_with_rows_carrying_their_own_validity():
    # Each row is an independent mini-QUESTION: validity is per row, and the
    # matrix as a whole carries none.
    payload = MatrixDetectionPayload(
        rows=[
            MatrixRowDetection(
                row_index=0,
                selected_cells=[MatrixCellDetection(col_index=1, confidence=0.9)],
                validity=Validity.VALID,
            ),
            MatrixRowDetection(
                row_index=1,
                selected_cells=[],
                validity=Validity.NO_ANSWER,
            ),
            MatrixRowDetection(
                row_index=2,
                selected_cells=[
                    MatrixCellDetection(col_index=0, confidence=0.81),
                    MatrixCellDetection(col_index=3, confidence=0.77),
                ],
                validity=Validity.TOO_MANY_ANSWERS,
            ),
        ]
    )

    assert [r.row_index for r in payload.rows] == [0, 1, 2]
    assert [r.validity for r in payload.rows] == [
        Validity.VALID,
        Validity.NO_ANSWER,
        Validity.TOO_MANY_ANSWERS,
    ]
    assert not hasattr(payload, "validity")


def test_matrix_payload_from_raw_jsonb_dict():
    raw = {
        "rows": [
            {
                "row_index": 0,
                "selected_cells": [{"col_index": 2, "confidence": 0.64}],
                "validity": "VALID",
            }
        ]
    }
    payload = MatrixDetectionPayload.model_validate(raw)

    assert payload.rows[0].selected_cells[0].col_index == 2
    assert payload.rows[0].selected_cells[0].confidence == pytest.approx(0.64)
    assert payload.rows[0].validity is Validity.VALID


def test_matrix_payload_round_trips_through_json_mode():
    payload = MatrixDetectionPayload(
        rows=[
            MatrixRowDetection(
                row_index=0,
                selected_cells=[MatrixCellDetection(col_index=1, confidence=0.42)],
                validity=Validity.VALID,
            )
        ]
    )
    dumped = payload.model_dump(mode="json")

    assert dumped == {
        "rows": [
            {
                "row_index": 0,
                "selected_cells": [{"col_index": 1, "confidence": 0.42}],
                "validity": "VALID",
            }
        ],
        # The column-oriented half of the payload is present and empty: under
        # a ROW rule the column groups are not merely empty, they do not
        # apply. Which of the two lists carries the reading is what says which
        # rule the object was authored with.
        "cols": [],
    }
    assert MatrixDetectionPayload.model_validate(dumped) == payload


def test_matrix_payload_with_no_rows_is_accepted():
    # A matrix whose rows were all unreadable is still a well-formed payload;
    # what to do about it is extraction/review logic, not schema logic.
    assert MatrixDetectionPayload(rows=[]).rows == []


# ---------------------------------------------------------------------------
# MATRIX — the column-oriented half
# ---------------------------------------------------------------------------


def test_matrix_payload_with_columns_carrying_their_own_validity():
    """Under ONE_PER_COLUMN / MANY_PER_COLUMN the COLUMN is the independent
    mini-QUESTION, so it is the column that carries the verdict — and its cells
    are referenced by ``row_index``, the mirror of the row-oriented case."""
    payload = MatrixDetectionPayload(
        cols=[
            MatrixColDetection(
                col_index=0,
                selected_cells=[MatrixRowCellDetection(row_index=2, confidence=0.88)],
                validity=Validity.VALID,
            ),
            MatrixColDetection(
                col_index=1, selected_cells=[], validity=Validity.NO_ANSWER
            ),
        ]
    )

    assert payload.rows == []
    assert [c.col_index for c in payload.cols] == [0, 1]
    assert payload.cols[0].selected_cells[0].row_index == 2
    assert not hasattr(payload, "validity")


def test_matrix_payload_from_raw_jsonb_dict_column_oriented():
    raw = {
        "cols": [
            {
                "col_index": 3,
                "selected_cells": [{"row_index": 1, "confidence": 0.55}],
                "validity": "VALID",
            }
        ]
    }
    payload = MatrixDetectionPayload.model_validate(raw)

    assert payload.cols[0].selected_cells[0].row_index == 1
    assert payload.cols[0].selected_cells[0].confidence == pytest.approx(0.55)
    assert payload.rows == []


def test_a_payload_written_before_the_columns_existed_still_validates():
    """Both lists default to empty, so a row-only JSON already sitting in the
    column keeps deserialising."""
    payload = MatrixDetectionPayload.model_validate(
        {"rows": [{"row_index": 0, "selected_cells": [], "validity": "NO_ANSWER"}]}
    )

    assert len(payload.rows) == 1
    assert payload.cols == []


def test_an_entirely_empty_matrix_payload_is_well_formed():
    """What a degenerate matrix (no rows or no columns authored) produces.
    Neither list applies, and that is a shape, not an error."""
    payload = MatrixDetectionPayload()
    assert payload.rows == [] and payload.cols == []


def test_matrix_row_cell_references_the_row_by_index_not_by_id():
    fields = MatrixRowCellDetection.model_fields
    assert "row_index" in fields
    assert "row_id" not in fields
    with pytest.raises(ValidationError):
        MatrixRowCellDetection.model_validate(
            {"row_id": str(uuid.uuid4()), "confidence": 0.9}
        )


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_a_column_cell_confidence_outside_the_unit_interval_is_refused(confidence):
    with pytest.raises(ValidationError):
        MatrixRowCellDetection(row_index=0, confidence=confidence)


def test_matrix_cell_references_the_column_by_index_not_by_id():
    # Guards the deliberate choice of col_index over a FK to MatrixColDef.id
    # (consistent with MatrixCellDef, where row/col are plain integers).
    fields = MatrixCellDetection.model_fields
    assert "col_index" in fields
    assert "col_id" not in fields
    with pytest.raises(ValidationError):
        MatrixCellDetection.model_validate(
            {"col_id": str(uuid.uuid4()), "confidence": 0.9}
        )
