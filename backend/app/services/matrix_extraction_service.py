# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Optical extraction of a MATRIX answer.

Deliberately the same machine as the QUESTION extractor, which establishes how
a scanned sheet is looked at — map the authored geometry onto the scan, sample
the disc of each printed bubble, let the fill ratio be both the selection test
and the confidence — and none of that is re-decided here. What a MATRIX adds is
a SECOND dimension and, with it, the question of what a "question" even is
inside a grid.

A row is a mini-QUESTION. Except when a column is.
----------------------------------------------------
The principle is "same logic as QUESTION, applied row by row". That holds for
three of the five selection rules, and the other two are not an exotic corner:
ONE_PER_COLUMN is how you author "assign each item to exactly one category, one
item per column". Under those rules the
constraint runs down the columns, so the GROUP that behaves like an independent
mini-QUESTION — the thing that carries a ``Validity`` — is the column.

Hence ``matrix_selection.is_row_oriented`` and the two symmetric halves of this module. The
payload mirrors the split: ``rows`` populated under a row rule, ``cols`` under a
column rule, never both (see ``app.schemas.submission_result``).

What every rule agrees on
-------------------------
**Zero selections in a group is NO_ANSWER, under every rule including FREE.**
FREE means "no constraint on how many cells may be marked", not "an unanswered
group is fine": nothing was read there, exactly as for a QUESTION nobody
answered, and NO_ANSWER is how that is said. FREE therefore behaves as
MANY_PER_ROW for validity purposes — no upper limit, only the empty case.

TOO_MANY_ANSWERS is asked of ``matrix_selection.is_single_selection`` rather
than re-derived from the rule here. That function already answers
"does this rule allow at most one cell per group", and it is the same question
authoring asks when it refuses to mark two correct cells in one row. Two
answers to it would eventually diverge, and the divergence would mean a sheet
the authoring tool called invalid being read as valid.

By INDEX, not by definition row
-------------------------------
Cells are walked over ``range(rows_count)`` × ``range(cols_count)``, not over
the ``MatrixRowDef`` / ``MatrixColDef`` rows. Those definitions carry LABELS and
are allowed to be missing — ``_render_matrix`` falls back to ``str(index)``
when they are — so the printed grid always has ``rows_count × cols_count``
bubbles whatever the definitions say. Iterating the definitions would make the
extractor skip a bubble that is physically on the sheet, which is the one thing
it must never do.

RESPONDENT_IDENTIFIER is not this function's business
-----------------------------------------------------
A MATRIX carrying that role is a respondent-ID grid: its cells spell a
candidate's id, they have no validity and belong to
``respondent_identifier_extraction_service``. This function REFUSES one
rather than handling it silently — the orchestrator's dispatch is what keeps it
away (``_resolve_extractor``), and a guess here would let a dispatch bug write
a wrong reading instead of raising.

Reuse, and what is borrowed from where
--------------------------------------
The template→scan mapping (``resolve_scan_region``/``_scale_factors``) and the
greyscale open are imported FROM ``question_extraction_service``: they are not
QUESTION-specific, they are how this pipeline reads any sheet, and a copy here
would be a second thing to keep in step. The pixel measurement itself lives in
``app.utils.bubble_sampling`` and the grid arithmetic in
``app.utils.matrix_layout``, since both are genuinely shared.
(Should a third extractor arrive, the three helpers still living in the QUESTION
module have earned a shared home of their own.)

Transactions: nothing here flushes or commits, the same discipline as the
QUESTION extractor — the caller owns the transaction, and the uniqueness of
``(submission_page_id, page_object_id)`` is enforced at ITS flush.
"""
from __future__ import annotations

from PIL import Image
from sqlalchemy.orm import Session

from app.models.enums import (
    MatrixSelectionRule,
    PageObjectRole,
    SubmissionResultType,
    Validity,
)
from app.models.page_object import PageObject
from app.models.submission_batch import SubmissionBatch
from app.models.submission_object_result import SubmissionObjectResult
from app.models.submission_page import SubmissionPage
from app.repositories import submission_object_result_repo
from app.schemas.submission_result import (
    MatrixCellDetection,
    MatrixColDetection,
    MatrixDetectionPayload,
    MatrixRowCellDetection,
    MatrixRowDetection,
)
from app.services import (
    confidence_threshold_service,
    matrix_selection,
    question_extraction_service,
)
from app.utils import bubble_sampling, matrix_layout, question_layout

# Which group (row or column) carries the validity: defined in the pure
# ``matrix_selection``, so scoring reads the same rule without
# importing the extractor. Re-exported here, where extraction and review use it.
is_row_oriented = matrix_selection.is_row_oriented


# ---------------------------------------------------------------------------
# Validity, per group
# ---------------------------------------------------------------------------


def compute_group_validity(
    rule: MatrixSelectionRule, n_selected: int
) -> Validity:
    """The discrete verdict on ONE group (row or column) of *n_selected* cells.

    Public for the same reason as its QUESTION counterpart: a SUBSTITUTE in
    review judges the cells the operator entered with it.

    The MATRIX counterpart of ``question_extraction_service.compute_validity``,
    and the same shape: nothing blackened is NO_ANSWER under EVERY rule, FREE
    included — "no constraint" limits how many cells may be marked, it does not
    make an unanswered group an answer. More than one cell is TOO_MANY_ANSWERS
    only for the single-selection rules, which ``matrix_selection`` already
    defines for authoring.
    """
    if n_selected == 0:
        return Validity.NO_ANSWER
    if matrix_selection.is_single_selection(rule) and n_selected > 1:
        return Validity.TOO_MANY_ANSWERS
    return Validity.VALID


# A private alias, still referenced by the extractor's unit tests.
_compute_group_validity = compute_group_validity


# ---------------------------------------------------------------------------
# Reading the cells
# ---------------------------------------------------------------------------


def _detect_selected_cells(
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

    Returns ``{(row_index, col_index): fill_ratio}`` for the SELECTED cells
    only — the absence of a coordinate is how "not selected" is expressed, the
    same contract ``QuestionDetectionPayload`` sets for a QUESTION's options.
    One dict for the whole grid, so grouping it by row or by column afterwards
    is free and, more to the point, cannot read a cell twice with two different
    answers.

    Geometry is walked in the TEMPLATE's pixel space and only each bubble's
    CENTRE is mapped onto *region* — the same choice the QUESTION extractor
    makes, so the coordinates are bit-for-bit the ones the export computed when
    it printed the circle.

    The sampling radius is scaled by the SMALLER of the two axis factors: under
    an anisotropic scan the printed circle is an ellipse, and the smaller factor
    keeps the sampling disc inside it rather than letting it spill onto the
    surrounding paper and dilute the ratio the confidence is made of.
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
                # The fill ratio does double duty: it decided the selection AND
                # it is the confidence of that reading. No separate score.
                selected[(row_index, col_index)] = fill_ratio
    return selected


def _build_rows(
    selected: dict[tuple[int, int], float],
    rule: MatrixSelectionRule,
    rows_count: int,
    cols_count: int,
) -> list[MatrixRowDetection]:
    """Group the readings by ROW — one mini-QUESTION per row.

    EVERY row is reported, including the ones nothing was marked on: their
    verdict is NO_ANSWER, and dropping them would lose the difference between a
    row the candidate skipped and a row that was never printed.
    """
    rows: list[MatrixRowDetection] = []
    for row_index in range(rows_count):
        cells = [
            MatrixCellDetection(
                col_index=col_index, confidence=selected[(row_index, col_index)]
            )
            for col_index in range(cols_count)
            if (row_index, col_index) in selected
        ]
        rows.append(
            MatrixRowDetection(
                row_index=row_index,
                selected_cells=cells,
                validity=compute_group_validity(rule, len(cells)),
            )
        )
    return rows


def _build_cols(
    selected: dict[tuple[int, int], float],
    rule: MatrixSelectionRule,
    rows_count: int,
    cols_count: int,
) -> list[MatrixColDetection]:
    """Group the readings by COLUMN — the mirror image of :func:`_build_rows`."""
    cols: list[MatrixColDetection] = []
    for col_index in range(cols_count):
        cells = [
            MatrixRowCellDetection(
                row_index=row_index, confidence=selected[(row_index, col_index)]
            )
            for row_index in range(rows_count)
            if (row_index, col_index) in selected
        ]
        cols.append(
            MatrixColDetection(
                col_index=col_index,
                selected_cells=cells,
                validity=compute_group_validity(rule, len(cells)),
            )
        )
    return cols


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def extract_matrix_result(
    db: Session,
    submission_page: SubmissionPage,
    page_object: PageObject,
    batch: SubmissionBatch,
) -> SubmissionObjectResult:
    """Extract the reading of one evaluable MATRIX object off one scanned page.

    Same signature and same contract as ``extract_question_result``, so the
    orchestrator's dispatch table stays a plain dict lookup.

    Preconditions, all raising rather than guessing: *submission_page* must be
    MATCHED to a template page (matching runs first), *page_object* must carry
    a ``matrix_config``, and it must NOT hold the RESPONDENT_IDENTIFIER role — a
    respondent-ID grid belongs to ``respondent_identifier_extraction_service``,
    and reading it as a set of answers would attribute a candidate's id to
    their answers.

    Builds and STAGES a ``SubmissionObjectResult`` carrying:

      * ``detected_payload_json`` — a ``MatrixDetectionPayload``: one group
        per row OR one per column depending on the selection rule, each with
        the cells read as selected, their confidences, and the group's own
        ``Validity``;
      * ``confidence_score`` — the MINIMUM confidence among ALL the selected
        cells of ALL the groups. The matrix is one result and is only as sure
        as its least sure bubble, exactly as a question is; NULL — not 0.0 —
        when nothing at all was marked;
      * ``validity_forces_review`` — True when AT LEAST ONE group is not VALID.
        Stated in concrete terms: an object whose row 3 has two marks needs a
        human even though its other nine rows are perfect;
      * ``review_required`` — the two axes above OR-ed under the batch's
        effective threshold, via ``confidence_threshold_service``.

    A degenerate matrix (``rows_count`` or ``cols_count`` not positive) is NOT
    an error: the export prints no grid for it, so there is nothing on the sheet
    to read. It yields an empty payload, a NULL confidence and no forced review
    — the same defensive guard ``_render_matrix`` applies, on the other side of
    the same contract.

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
            "extract_matrix_result only handles MATRIX objects"
        )
    if page_object.effective_role is PageObjectRole.RESPONDENT_IDENTIFIER:
        raise ValueError(
            f"PageObject {page_object.id} is a RESPONDENT_IDENTIFIER matrix "
            "(a respondent-ID grid), not an evaluable question: "
            "extract_matrix_result must not be called on it"
        )

    rows_count = config.rows_count
    cols_count = config.cols_count
    rule = config.selection_rule

    # The degenerate matrix, decided once and up front: a grid is printed only
    # when BOTH counts are positive, which is the guard ``_render_matrix``
    # applies before drawing anything. Zero columns is as degenerate as zero
    # rows — reporting N empty NO_ANSWER rows for a matrix that has no cells at
    # all would invent an unanswered question out of an object nobody could ever
    # answer, and send it to review. Nothing printed, nothing read: the empty
    # payload built here is the whole result.
    groups: list[MatrixRowDetection] | list[MatrixColDetection] = []
    payload = MatrixDetectionPayload()
    selected: dict[tuple[int, int], float] = {}

    if rows_count > 0 and cols_count > 0:
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
        selected = _detect_selected_cells(
            gray_image,
            page_object,
            geometry,
            region,
            scale_x,
            scale_y,
            rows_count,
            cols_count,
        )

        # Exactly one of the two lists is ever populated, and the rule is what
        # decides which — see the module docstring.
        if is_row_oriented(rule):
            groups = _build_rows(selected, rule, rows_count, cols_count)
            payload = MatrixDetectionPayload(rows=groups)
        else:
            groups = _build_cols(selected, rule, rows_count, cols_count)
            payload = MatrixDetectionPayload(cols=groups)

    result = SubmissionObjectResult(
        submission_page_id=submission_page.id,
        page_object_id=page_object.id,
        result_type=SubmissionResultType.MATRIX,
        detected_payload_json=payload.model_dump(mode="json"),
        # One result, one confidence: the least sure bubble of the whole grid,
        # whichever group it landed in. NULL when nothing was marked anywhere.
        confidence_score=min(selected.values()) if selected else None,
        # One non-VALID group is enough — see the docstring.
        validity_forces_review=any(
            group.validity is not Validity.VALID for group in groups
        ),
        sheet_not_aligned=question_extraction_service.sheet_not_aligned(
            submission_page
        ),
    )
    # Written from the two axes just set, under the threshold in force TODAY.
    # A later threshold change re-derives it and touches neither.
    result.review_required = confidence_threshold_service.compute_review_required(
        result, confidence_threshold_service.effective_threshold(batch)
    )
    return submission_object_result_repo.add(db, result)
