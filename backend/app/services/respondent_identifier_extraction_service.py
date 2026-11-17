# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Optical extraction of a respondent-ID grid.

A MATRIX carrying the RESPONDENT_IDENTIFIER role is not a question: its cells
spell a candidate's id, one digit per column, and it has no ``Validity`` — the
MATRIX extractor refuses one outright, and this module is what the
orchestrator's dispatch (``_resolve_extractor``) hands it to instead.

Column-oriented always, never by configuration
------------------------------------------------
``MatrixSelectionRule`` is not consulted here. A respondent-ID grid is
structurally one column per digit of the identifier BY DEFINITION — the same
fact whatever ``selection_rule`` the object happens to carry — unlike an
evaluable MATRIX, where the rule is what decides which axis
carries the mini-QUESTION.

Reuse, and what is borrowed from where
--------------------------------------
Exactly the machine the MATRIX extractor uses, and none of it re-implemented:
``matrix_layout.compute_grid_geometry``/``cell_center`` for the grid
arithmetic, ``bubble_sampling.sample_circle_fill_ratio`` and
``SELECTION_FILL_THRESHOLD`` for reading one cell, and the template→scan
mapping (``resolve_scan_region``/``_scale_factors``) plus the greyscale open
(``_open_gray_scan``) from ``question_extraction_service`` — the same imports
``matrix_extraction_service`` already uses for the same purpose.

A digit is a column's WINNING cell, by confidence
--------------------------------------------------
Zero cells selected in a column reads as ``"?"`` and contributes NO entry to
``digits`` — a missing digit is a signal, not silence, so its 0.0 still counts
toward the object's aggregated confidence (see below). One cell selected is
the ordinary case. More than one is an ambiguity: the cell with the higher
confidence wins (a tie goes to the lower ``row_index``, a deterministic and
arbitrary-but-stable choice), its label is what is written down, and the
column is ALSO listed in ``ambiguous_columns`` so the anomaly is never
silently lost.

Confidence is the MEAN over every column, not the minimum
-----------------------------------------------------------
Unlike an evaluable MATRIX (whose ``confidence_score`` is the minimum over its
selected cells), a respondent-ID grid averages over ALL ``cols_count`` columns,
including the 0.0 of an empty one. A missing digit of an id is itself a bad
sign — unlike a QUESTION option nobody was forced to select, every column of
an identifier is expected to carry exactly one mark, so treating an empty one
as "not applicable" and excluding it from the average would hide precisely the
column most in need of a human's eyes.

Three cases force review, whatever the confidence
--------------------------------------------------
There is no ``Validity`` axis for this result type, but ``validity_forces_review``
is not about Validity alone: it means "a human is needed whatever the
confidence". It is set True here in exactly three cases:

  * an AMBIGUOUS column (``ambiguous_columns`` not empty). The column's digit
    was chosen by this module, not written by the candidate — and with two
    clean marks the mean confidence stays high, so the confidence axis alone
    would auto-accept an identifier the system made up one digit of;
  * an EMPTY column (fewer ``digits`` than columns, a "?" in ``raw_value``).
    One blank column out of six still leaves the mean around
    0.83, above the default threshold, yet the identifier can be neither
    confirmed nor used as the candidate: without the flag nobody would look;
  * the DEGENERATE grid (see below): nothing could be read at all.

Why this strictly: the candidate attribution is the most sensitive fact of a
submission. A wrong answer to one question costs a point; a wrong respondent
ID hands every answer of the submission to the wrong person. Every case goes
through the AXIS, not through ``review_required`` directly, because the
threshold recomputation (``confidence_threshold_service``) re-derives
``review_required`` from the two axes only — a flag forced on the side would
be silently cleared by the next threshold change. ``review_required`` itself is
derived exactly as for every other type.

The degenerate grid
--------------------
``rows_count`` or ``cols_count`` not positive means the export printed no
grid, mirroring the guard ``_render_matrix``/``matrix_extraction_service``
already apply. Here it is not merely "nothing to read": a respondent-ID grid
structurally wrong is worth a human's attention, so it forces review through
``validity_forces_review`` as above. Its payload is recognisable on its own: an
EMPTY ``raw_value`` (a real grid always writes one character per column, "?"
included), no digits and a NULL confidence.

No backfill
-----------
Results stored before these rules existed keep their old values: a degenerate
grid with ``review_required`` forced but both axes neutral, an ambiguous or an
incomplete one with ``validity_forces_review`` False. Nothing migrates them. To
regenerate them (test environments), delete the batch's results and re-run
extraction.

Transactions: nothing here flushes or commits, the same discipline as the
QUESTION and MATRIX extractors — the caller owns the transaction.
"""
from __future__ import annotations

from PIL import Image
from sqlalchemy.orm import Session

from app.models.enums import PageObjectRole, SubmissionResultType
from app.models.page_object import PageObject
from app.models.submission_batch import SubmissionBatch
from app.models.submission_object_result import SubmissionObjectResult
from app.models.submission_page import SubmissionPage
from app.repositories import submission_object_result_repo
from app.schemas.submission_result import (
    DigitDetection,
    RespondentIdentifierDetectionPayload,
)
from app.services import confidence_threshold_service, question_extraction_service
from app.utils import bubble_sampling, matrix_layout, question_layout

# ---------------------------------------------------------------------------
# Reading the cells
# ---------------------------------------------------------------------------


def _sample_selected_cells(
    gray_image: Image.Image,
    page_object: PageObject,
    geometry: matrix_layout.MatrixGridGeometry,
    region: question_extraction_service.ScanRegion,
    scale_x: float,
    scale_y: float,
    rows_count: int,
    cols_count: int,
) -> dict[tuple[int, int], float]:
    """Sample every printed cell once; keep the ones read as blackened.

    Identical in shape to ``matrix_extraction_service._detect_selected_cells``
    — same primitives, same radius rule (scaled by the SMALLER axis factor so
    an anisotropic scan's sampling disc stays inside the printed ellipse) —
    kept as its own small loop here rather than imported, the same way each
    extractor owns its own thin sampling pass over the shared primitives.
    """
    radius = question_layout.CIRCLE_R * min(scale_x, scale_y)

    selected: dict[tuple[int, int], float] = {}
    for row_index in range(rows_count):
        for col_index in range(cols_count):
            cx, cy = matrix_layout.cell_center(geometry, row_index, col_index)
            fill_ratio = bubble_sampling.sample_circle_fill_ratio(
                gray_image,
                region.x + (cx - page_object.x) * scale_x,
                region.y + (cy - page_object.y) * scale_y,
                radius,
            )
            if fill_ratio > bubble_sampling.SELECTION_FILL_THRESHOLD:
                selected[(row_index, col_index)] = fill_ratio
    return selected


def _resolve_column(
    col_index: int,
    selected: dict[tuple[int, int], float],
    rows_count: int,
    row_labels: dict[int, str],
) -> tuple[str, DigitDetection | None, bool]:
    """The reading of ONE column: its character, its digit entry, and whether
    it was ambiguous (more than one cell selected in it).

    Zero candidates -> ``("?", None, False)``: nothing to report, and the
    caller is what turns the absence into a 0.0 confidence contribution.
    """
    candidates = [
        (row_index, selected[(row_index, col_index)])
        for row_index in range(rows_count)
        if (row_index, col_index) in selected
    ]
    if not candidates:
        return "?", None, False

    ambiguous = len(candidates) > 1
    # Highest confidence wins; a tie goes to the lower row_index.
    winner_row, winner_confidence = max(candidates, key=lambda rc: (rc[1], -rc[0]))
    label = row_labels.get(winner_row, str(winner_row))
    digit = DigitDetection(
        col_index=col_index,
        row_index=winner_row,
        label=label,
        confidence=winner_confidence,
    )
    return label, digit, ambiguous


def _build_digits(
    selected: dict[tuple[int, int], float],
    rows_count: int,
    cols_count: int,
    row_labels: dict[int, str],
) -> tuple[str, list[DigitDetection], list[int], float]:
    """Walk every column in order and assemble the reading of the whole grid.

    Returns ``(raw_value, digits, ambiguous_columns, confidence_score)``.
    ``confidence_score`` is the arithmetic MEAN of every column's contribution
    over ``cols_count`` — an empty column contributes 0.0 and is NOT excluded,
    unlike a QUESTION option nobody selected (see the module docstring).
    """
    chars: list[str] = []
    digits: list[DigitDetection] = []
    ambiguous_columns: list[int] = []
    confidence_sum = 0.0

    for col_index in range(cols_count):
        char, digit, ambiguous = _resolve_column(
            col_index, selected, rows_count, row_labels
        )
        chars.append(char)
        if digit is not None:
            digits.append(digit)
            confidence_sum += digit.confidence
        if ambiguous:
            ambiguous_columns.append(col_index)

    return "".join(chars), digits, ambiguous_columns, confidence_sum / cols_count


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def extract_respondent_identifier_result(
    db: Session,
    submission_page: SubmissionPage,
    page_object: PageObject,
    batch: SubmissionBatch,
) -> SubmissionObjectResult:
    """Extract the respondent ID read off one MATRIX/RESPONDENT_IDENTIFIER
    object.

    Preconditions, all raising rather than guessing: *submission_page* must be
    MATCHED to a template page, *page_object* must carry a ``matrix_config``,
    and it must hold the RESPONDENT_IDENTIFIER role — this function is
    defensive about the last one because if it is ever called on a MATRIX
    without that role, dispatch has already gone wrong upstream and the right
    response is to raise, not to guess which kind of object this is.

    Builds and STAGES a ``SubmissionObjectResult`` carrying:

      * ``detected_payload_json`` — a ``RespondentIdentifierDetectionPayload``:
        the reconstructed ``raw_value``, one ``DigitDetection`` per column
        that had a selection, and any ambiguous columns;
      * ``confidence_score`` — the MEAN over every column of the grid
        (0.0 for an empty one), NULL only in the degenerate case below;
      * ``validity_forces_review`` — True when a column is ambiguous or empty,
        or the grid is degenerate, False otherwise (see the module docstring);
      * ``review_required`` — via ``confidence_threshold_service``, like every
        other extractor, so the cases above survive any threshold change.

    A degenerate grid (``rows_count`` or ``cols_count`` not positive) is not an
    error: the export prints nothing, so there is nothing on the sheet to
    read. It yields an empty ``raw_value``, no digits, a NULL confidence, and
    ``validity_forces_review=True`` — a structurally broken object is always
    worth a human's attention, confidence aside.

    Does NOT flush or commit: the caller owns the transaction.
    """
    if submission_page.form_variant_page_id is None:
        raise ValueError(
            f"SubmissionPage {submission_page.id} is not matched to a template "
            "page: extraction needs form_variant_page_id, which matching "
            "must have resolved first"
        )
    config = page_object.matrix_config
    if config is None:
        raise ValueError(
            f"PageObject {page_object.id} has no matrix_config: "
            "extract_respondent_identifier_result only handles MATRIX objects"
        )
    if page_object.effective_role is not PageObjectRole.RESPONDENT_IDENTIFIER:
        raise ValueError(
            f"PageObject {page_object.id} does not hold the "
            "RESPONDENT_IDENTIFIER role: extract_respondent_identifier_result "
            "must not be called on an evaluable MATRIX (extract_matrix_result "
            "is what reads those)"
        )

    rows_count = config.rows_count
    cols_count = config.cols_count

    if rows_count <= 0 or cols_count <= 0:
        payload = RespondentIdentifierDetectionPayload(
            raw_value="", digits=[], ambiguous_columns=[]
        )
        result = SubmissionObjectResult(
            submission_page_id=submission_page.id,
            page_object_id=page_object.id,
            result_type=SubmissionResultType.RESPONDENT_IDENTIFIER,
            detected_payload_json=payload.model_dump(mode="json"),
            confidence_score=None,
            # On the axis, not on review_required: a grid with no rows or no
            # columns is structurally wrong, not merely "unsure", and the
            # threshold recomputation must never clear that.
            validity_forces_review=True,
            sheet_not_aligned=question_extraction_service.sheet_not_aligned(
                submission_page
            ),
        )
        result.review_required = confidence_threshold_service.compute_review_required(
            result, confidence_threshold_service.effective_threshold(batch)
        )
        return submission_object_result_repo.add(db, result)

    gray_image = question_extraction_service._open_gray_scan(submission_page)
    template_page = submission_page.form_variant_page
    scale_x, scale_y = question_extraction_service._scale_factors(
        template_page, *gray_image.size
    )
    region = question_extraction_service.resolve_scan_region(
        page_object, template_page, *gray_image.size
    )
    geometry = matrix_layout.compute_grid_geometry(
        page_object.x,
        page_object.y,
        page_object.width,
        page_object.height,
        rows_count,
        cols_count,
    )
    selected = _sample_selected_cells(
        gray_image,
        page_object,
        geometry,
        region,
        scale_x,
        scale_y,
        rows_count,
        cols_count,
    )
    row_labels = {row.order_index: row.label for row in config.rows}

    raw_value, digits, ambiguous_columns, confidence_score = _build_digits(
        selected, rows_count, cols_count, row_labels
    )

    payload = RespondentIdentifierDetectionPayload(
        raw_value=raw_value, digits=digits, ambiguous_columns=ambiguous_columns
    )
    result = SubmissionObjectResult(
        submission_page_id=submission_page.id,
        page_object_id=page_object.id,
        result_type=SubmissionResultType.RESPONDENT_IDENTIFIER,
        detected_payload_json=payload.model_dump(mode="json"),
        confidence_score=confidence_score,
        # A digit chosen between two marks is the system's guess, not the
        # candidate's answer; a column with no digit leaves an identifier
        # nobody can confirm. Either way a human looks, however confident the
        # other marks.
        validity_forces_review=bool(ambiguous_columns) or len(digits) < cols_count,
        sheet_not_aligned=question_extraction_service.sheet_not_aligned(
            submission_page
        ),
    )
    result.review_required = confidence_threshold_service.compute_review_required(
        result, confidence_threshold_service.effective_threshold(batch)
    )
    return submission_object_result_repo.add(db, result)
