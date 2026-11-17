# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Confidence/Validity data contract for extraction results.

These schemas describe the SHAPE of
``SubmissionObjectResult.detected_payload_json`` — the JSONB column whose
content varies by ``result_type``. Nothing here adds a column or a migration:
the contract lives entirely inside that existing JSON.

They are the CONTRACT that the producer (the extraction pipeline) respects
when it WRITES that JSON, and that a consumer (scoring, review) can use to
validate/deserialize what it READS. ``SubmissionObjectResultRead`` does NOT
reference these models: its ``detected_payload_json`` stays
``dict[str, Any] | None`` in the public API.

Two independent axes are expressed here:

  * **Confidence** — a continuous per-reading measure in ``[0, 1]``, attached
    to each option/cell read as SELECTED. It answers "how sure is the optical
    reading that this bubble is blackened".
  * **Validity** — a discrete per-question measure (``app.models.enums.Validity``).
    It answers "does the combination of options read respect the rules of the
    question".

They are NOT derived from one another, at any level — including this one: no
schema-level validation ties an empty ``selected_options`` to
``Validity.NO_ANSWER``. Deducing one axis from the other is extraction logic
and belongs to the extractors, not to the data contract.

This module defines only the FORM of the data; it computes nothing.

The second half of the contract is the REVIEWED payloads — the shape of
``reviewed_payload_json``, written when an operator substitutes a reading —
and the per-type input a SUBSTITUTE request carries. See the section at the
end of the module.
"""
from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import Validity


# ---------------------------------------------------------------------------
# QUESTION
# ---------------------------------------------------------------------------


class OptionDetection(BaseModel):
    """A single option read as SELECTED (blackened bubble), with the
    confidence of the optical reading.

    Confidence is computed ONLY for options actually blackened: there is no
    entry here for empty options — consistent with the extraction rule that
    "no confidence is computed/stored for non-selected options". The absence of
    an option from ``selected_options`` is therefore the way "not selected" is
    represented, not a zero-confidence entry.
    """

    # References QuestionOption.id.
    option_id: uuid.UUID
    confidence: float = Field(ge=0, le=1)


class QuestionDetectionPayload(BaseModel):
    """Shape of ``detected_payload_json`` when
    ``SubmissionObjectResult.result_type == QUESTION``.

    ``selected_options`` may legitimately be empty (nothing blackened);
    ``validity`` is a separate, independently-assigned axis.
    """

    selected_options: list[OptionDetection]
    validity: Validity


# ---------------------------------------------------------------------------
# MATRIX
# ---------------------------------------------------------------------------


class MatrixCellDetection(BaseModel):
    """A SELECTED cell of a MATRIX row, with the confidence of the optical
    reading.

    The column is referenced by ``order_index`` (``MatrixColDef.order_index``),
    not by id — consistent with how cells are indexed in ``MatrixCellDef``,
    where ``row_index``/``col_index`` are plain integers and not FKs.
    """

    col_index: int
    confidence: float = Field(ge=0, le=1)


class MatrixRowDetection(BaseModel):
    """A MATRIX row treated as an independent mini-QUESTION.

    Same logic as QUESTION, applied row by row — the principle stated in the
    functional document. Hence a row carries its own ``validity``: validity is
    a per-row verdict, and the matrix as a whole has none.
    """

    row_index: int
    selected_cells: list[MatrixCellDetection]
    validity: Validity


class MatrixRowCellDetection(BaseModel):
    """A cell read as SELECTED inside a COLUMN group, referenced by its row.

    The mirror image of :class:`MatrixCellDetection`, which references a cell by
    ``col_index`` inside a ROW group. Two classes rather than one with both
    indices because a group already fixes one of the two coordinates: repeating
    it on every cell would make two places able to disagree about which group
    the cell belongs to.
    """

    row_index: int
    confidence: float = Field(ge=0, le=1)


class MatrixColDetection(BaseModel):
    """A MATRIX column treated as an independent mini-QUESTION.

    Symmetric to :class:`MatrixRowDetection`, and used instead of it when the
    object's ``selection_rule`` is ONE_PER_COLUMN / MANY_PER_COLUMN: under
    those rules the constraint — and therefore the ``validity`` verdict — is
    per column, so the column is the group that carries it.
    """

    col_index: int
    selected_cells: list[MatrixRowCellDetection]
    validity: Validity


class MatrixDetectionPayload(BaseModel):
    """Shape of ``detected_payload_json`` when
    ``SubmissionObjectResult.result_type == MATRIX`` — the evaluable object,
    NOT a MATRIX acting as RESPONDENT_IDENTIFIER: that case carries no
    Validity and has its own payload,
    :class:`RespondentIdentifierDetectionPayload`.

    ``rows`` is populated for ONE_PER_ROW / MANY_PER_ROW / FREE, ``cols`` for
    ONE_PER_COLUMN / MANY_PER_COLUMN. EXACTLY ONE of the two is populated,
    never both, and which one is decided by the object's ``selection_rule``.
    The empty list is not an alternative encoding of "no selection": under a
    row rule an unmarked matrix still reports every row, each with an empty
    ``selected_cells`` and NO_ANSWER. Both default to empty
    so a degenerate matrix (zero rows or zero columns) has a well-formed
    payload, and so a payload written before this field existed still
    validates.
    """

    rows: list[MatrixRowDetection] = []
    cols: list[MatrixColDetection] = []


# ---------------------------------------------------------------------------
# RESPONDENT_IDENTIFIER
# ---------------------------------------------------------------------------


class DigitDetection(BaseModel):
    """One COLUMN of a respondent-ID grid with at least one cell read as SELECTED
    — the winning reading (by confidence, when more than one cell of the
    column was marked) and its confidence.

    A column that carries no selection at all has no entry here: the absence
    of a ``col_index`` from ``digits`` is how "nothing read in this column" is
    represented, the same contract :class:`OptionDetection` sets for a
    QUESTION option never blackened.
    """

    col_index: int
    row_index: int
    # ``MatrixRowDef.label`` of the winning row, or ``str(row_index)`` when the
    # grid carries no definition for it — the same tolerance
    # ``_render_matrix``'s ``row_labels.get(rr, str(rr))`` already applies.
    label: str
    confidence: float = Field(ge=0, le=1)


class RespondentIdentifierDetectionPayload(BaseModel):
    """Shape of ``detected_payload_json`` when
    ``SubmissionObjectResult.result_type == RESPONDENT_IDENTIFIER`` — a
    respondent-ID grid, NOT an evaluable question: it carries no ``Validity``,
    unlike :class:`MatrixDetectionPayload`.

    The grid is read PER COLUMN regardless of the object's
    ``MatrixSelectionRule``: a respondent-ID grid is structurally column-oriented
    by definition (one column per digit of the identifier), not by
    configuration.
    """

    # The reconstructed identifier: one character per column, in column order.
    # A column with no selection contributes "?" rather than being omitted, so
    # the string always has exactly ``cols_count`` characters.
    raw_value: str
    # Only the columns that had at least one selection.
    digits: list[DigitDetection]
    # Columns with MORE than one cell selected — resolved deterministically
    # (highest confidence, then lowest row_index) but reported here as an
    # anomaly, never silently lost. Not empty → the result forces review
    # (``validity_forces_review``): the winning digit is the system's choice.
    ambiguous_columns: list[int] = []


# ---------------------------------------------------------------------------
# FREE_TEXT
# ---------------------------------------------------------------------------


class FreeTextDetectionPayload(BaseModel):
    """Shape of ``detected_payload_json`` when
    ``SubmissionObjectResult.result_type == FREE_TEXT``.

    No ``Validity`` field: a free-text answer is not an evaluable question, and
    ``review_required`` is always True by design, whatever either axis says.
    ``suggested_text`` is what the OCR read — a SUGGESTION for the reviewer,
    never an accepted answer — and may be empty when nothing legible was found.
    """

    suggested_text: str


# ---------------------------------------------------------------------------
# CODE
# ---------------------------------------------------------------------------


class CodeDetectionPayload(BaseModel):
    """Shape of ``detected_payload_json`` when
    ``SubmissionObjectResult.result_type == CODE`` — a role-less CODE, i.e.
    printed content. A CODE holding a scanning role is consumed by matching
    and never produces a result.

    No ``Validity`` field and no graduated confidence: a QR / barcode decode
    either passes the format's own checksum or it does not, so the only
    outcome is binary.
    """

    # The raw decoded payload; None exactly when ``decode_successful`` is False.
    decoded_value: str | None
    decode_successful: bool


# ===========================================================================
# REVIEWED payloads
#
# The shape of ``SubmissionObjectResult.reviewed_payload_json`` — the value an
# operator entered with a SUBSTITUTE — and of the same value copied onto its
# ``SubmissionReview`` log row. One shape per result type, chosen by the
# ``result_type`` with ONE exception: any object holding the
# RESPONDENT_IDENTIFIER role (a respondent-ID grid, or a FREE_TEXT_AREA with that
# role) takes :class:`RespondentIdentifierReviewedPayload`, because what the
# operator enters there is an identity, not an answer.
#
# A placeholder ("Review anyway") takes the shape of its ``result_type``, like
# any reading of the same object would.
#
# ``validity`` is ALWAYS computed by the server — with the very functions the
# extractors use — never accepted from the client: the operator says what is
# marked on the paper, the rules of the question say whether that is valid.
#
# No confidences: a human's reading of the paper is not an optical measure.
# ===========================================================================


class QuestionReviewedPayload(BaseModel):
    """Reviewed QUESTION: the options the operator says are marked, in the
    question's option order. Empty means "the candidate did not answer"
    (NO_ANSWER) — distinct from a FLAG, which means "cannot tell"."""

    selected_option_ids: list[uuid.UUID]
    validity: Validity


class MatrixRowReview(BaseModel):
    """One ROW of a reviewed MATRIX, under a row-oriented rule."""

    row_index: int
    selected_col_indices: list[int]
    validity: Validity


class MatrixColReview(BaseModel):
    """One COLUMN of a reviewed MATRIX, under a column-oriented rule."""

    col_index: int
    selected_row_indices: list[int]
    validity: Validity


class MatrixReviewedPayload(BaseModel):
    """Reviewed MATRIX, grouped EXACTLY like :class:`MatrixDetectionPayload`:
    ``rows`` under a row-oriented rule, ``cols`` under a column-oriented one
    (``matrix_selection.is_row_oriented``), never both, and EVERY
    group listed — an unmarked one with an empty selection and NO_ANSWER."""

    rows: list[MatrixRowReview] = []
    cols: list[MatrixColReview] = []


class RespondentIdentifierReviewedPayload(BaseModel):
    """Reviewed candidate identifier — for a respondent-ID grid and for a
    FREE_TEXT_AREA holding RESPONDENT_IDENTIFIER alike. Trimmed, never empty."""

    value: str


class FreeTextReviewedPayload(BaseModel):
    """Reviewed free-text answer, trimmed. May be empty: the candidate wrote
    nothing."""

    text: str


class CodeReviewedPayload(BaseModel):
    """Reviewed CODE value, trimmed, never empty."""

    value: str


# ---------------------------------------------------------------------------
# SUBSTITUTE input — what the client sends as ``value``, per type
#
# Strict (``extra="forbid"``): a value meant for another type is refused, not
# half-read. Range, ownership and duplicate checks need the object's config,
# so they live in ``review_action_service``, not here.
# ---------------------------------------------------------------------------


class _StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QuestionSubstitution(_StrictInput):
    # Options of THIS question; no duplicates; empty allowed (no answer).
    selected_option_ids: list[uuid.UUID]


class MatrixSubstitution(_StrictInput):
    # ``[row_index, col_index]`` pairs, in range; no duplicates; empty allowed.
    # The server builds the groups.
    selected_cells: list[tuple[int, int]]


class RespondentIdentifierSubstitution(_StrictInput):
    value: str


class FreeTextSubstitution(_StrictInput):
    text: str


class CodeSubstitution(_StrictInput):
    value: str
