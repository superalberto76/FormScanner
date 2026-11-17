# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The columns per question of the Detail export.

Pure: it reads the authored objects of a variant and the results of one
submission, already loaded, and says which columns exist and what each cell
holds. ``batch_export_service`` loads, ``export_writers`` writes.

Positions
---------
A position is an object — or one group of a MATRIX — in READING ORDER: page,
then top to bottom, then left to right, as the review lists the objects
(``(page_index, y, x, id)``). An object linked across pages is ONE position,
at its first copy in that order; the groups of a MATRIX (rows, or columns
under a column rule) are consecutive positions, in their order.

Families, each numbered by position from 1:

  * ``D`` — QUESTION and the groups of an evaluative MATRIX
    (``scoring_rules.is_scored_matrix``): two columns, ``Dn`` and ``Dn_pt``;
  * ``M`` — the groups of a MATRIX that is not evaluative: the answer only;
  * ``T`` — FREE_TEXT_AREA: the text;
  * ``C`` — CODE: the value.

An object holding a role (RESPONDENT_IDENTIFIER, VARIANT_IDENTIFIER, own or of
its linked group) has no column: the candidate is already a column of the
Summary export. Neither has a QUESTION or MATRIX without its config (nothing
authored to answer, and it does not count for the score either).

Cells
-----
Which reading: the FIRST in physical page order
(``scoring_rules.first_readings``), the one the score counts, even when it is
FLAGGED or pending and a later one is answered. It is
read against the copy it was read on — linked copies have options of their
own — while the column sits at the first copy in layout order.

  * Answers as the review shows them: the option letters by position, "A" the
    first (``option_letter``), several joined by ", " ("A, C"); for a MATRIX
    group the labels of the marked columns (rows under a column rule), the
    1-based index when a label is missing.
  * :data:`NO_ANSWER` (``*``) for every "no answer": the candidate's NO_ANSWER
    (or nothing marked), FLAGGED, still pending, and an object with no reading
    at all (its page is missing). An empty cell means "this column does not
    exist for this submission" — never "no answer".
  * ``Dn_pt`` is what scoring gives that question (``score_question_result``,
    the groups of ``score_matrix_result``): 0 for every "no answer", a FLAG on
    a MATRIX zeroes all its groups, FULL_ANSWER and TOO_MANY_ANSWERS show the
    letters marked with their score. The ``Dn_pt`` of a submission add up to
    its points.
  * ``T`` and ``C`` hold verified values only: the text or value confirmed,
    substituted, or — for a CODE — decoded with no review needed. Anything
    else, an empty text included, is ``*``.

A broken invariant on one result (``ValueError`` from scoring or from a
payload that does not validate) is logged and leaves THAT object's cells
empty: the export does not fail on one bad row, as the review page does not.
"""
from __future__ import annotations

import enum
import logging
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from app.models.enums import PageObjectType, ReviewOutcome, Validity
from app.models.page_object import PageObject
from app.models.submission_object_result import SubmissionObjectResult
from app.schemas.submission_result import (
    CodeDetectionPayload,
    CodeReviewedPayload,
    FreeTextDetectionPayload,
    FreeTextReviewedPayload,
)
from app.services import scoring_rules
from app.services.matrix_selection import is_row_oriented

logger = logging.getLogger(__name__)

NO_ANSWER = "*"

Cell = str | float | None


class Family(str, enum.Enum):
    """The column families, in the order they appear in the file."""

    D = "D"
    M = "M"
    T = "T"
    C = "C"


FAMILY_ORDER: tuple[Family, ...] = (Family.D, Family.M, Family.T, Family.C)


@dataclass(frozen=True)
class Position:
    """One position of a variant: an object, or one group of a MATRIX.

    ``page_object`` is the copy the position sits at (the first in reading
    order); ``group`` the row or column index of a MATRIX group, ``None``
    otherwise. ``page_index`` is the 1-based template page.
    """

    family: Family
    page_object: PageObject
    group: int | None
    page_index: int


def option_letter(index: int) -> str:
    """"A", "B", … "Z", then "AA", "AB" … — a 0-based option position as a
    letter, as the review shows it (``optionLetter`` in the FE)."""
    letters = ""
    n = index
    while True:
        letters = chr(65 + n % 26) + letters
        n = n // 26 - 1
        if n < 0:
            return letters


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------


def _family(page_object: PageObject) -> Family | None:
    if page_object.effective_role is not None:
        return None
    kind = page_object.object_type
    if kind is PageObjectType.QUESTION:
        return Family.D if page_object.question_config is not None else None
    if kind is PageObjectType.MATRIX:
        if page_object.matrix_config is None:
            return None
        return Family.D if scoring_rules.is_scored_matrix(page_object) else Family.M
    if kind is PageObjectType.FREE_TEXT_AREA:
        return Family.T
    if kind is PageObjectType.CODE:
        return Family.C
    return None


def _reading_order(page_object: PageObject) -> tuple[int, float, float, str]:
    return (
        page_object.form_variant_page.page_index,
        page_object.y,
        page_object.x,
        str(page_object.id),
    )


def _matrix_groups(page_object: PageObject) -> int:
    config = page_object.matrix_config
    if is_row_oriented(config.selection_rule):
        return config.rows_count
    return config.cols_count


def variant_positions(page_objects: Iterable[PageObject]) -> list[Position]:
    """The positions of one variant, every family together, in reading order
    (module docstring). *page_objects* are its objects, each with its
    ``form_variant_page`` loaded; objects without a column are skipped."""
    seen: set[uuid.UUID] = set()
    positions: list[Position] = []
    for page_object in sorted(page_objects, key=_reading_order):
        family = _family(page_object)
        if family is None:
            continue
        key = scoring_rules.reading_key(page_object)
        if key in seen:
            continue
        seen.add(key)
        page_index = page_object.form_variant_page.page_index
        if page_object.object_type is PageObjectType.MATRIX:
            positions.extend(
                Position(family, page_object, group, page_index)
                for group in range(_matrix_groups(page_object))
            )
        else:
            positions.append(Position(family, page_object, None, page_index))
    return positions


def family_counts(positions: Iterable[Position]) -> dict[Family, int]:
    counts = {family: 0 for family in FAMILY_ORDER}
    for position in positions:
        counts[position.family] += 1
    return counts


# ---------------------------------------------------------------------------
# Layout of the columns
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Layout:
    """How many positions each family has in the file: the most any variant
    of the batch has. Shorter variants leave the rest empty."""

    counts: Mapping[Family, int]

    @classmethod
    def of(cls, variants: Iterable[Sequence[Position]]) -> Layout:
        counts = {family: 0 for family in FAMILY_ORDER}
        for positions in variants:
            for family, count in family_counts(positions).items():
                counts[family] = max(counts[family], count)
        return cls(counts=counts)

    def _width(self, family: Family) -> int:
        return self.counts[family] * (2 if family is Family.D else 1)

    def offset(self, family: Family) -> int:
        """The first cell of *family* among the columns per question."""
        offset = 0
        for other in FAMILY_ORDER:
            if other is family:
                return offset
            offset += self._width(other)
        raise ValueError(family)

    @property
    def width(self) -> int:
        return sum(self._width(family) for family in FAMILY_ORDER)

    def headers(self) -> tuple[str, ...]:
        """``D1``, ``D1_pt``, … ``M1``, … ``T1``, … ``C1``, …"""
        headers: list[str] = []
        for family in FAMILY_ORDER:
            for number in range(1, self.counts[family] + 1):
                headers.append(f"{family.value}{number}")
                if family is Family.D:
                    headers.append(f"{family.value}{number}_pt")
        return tuple(headers)

    def names(self) -> tuple[str, ...]:
        """One name per position, without the ``_pt`` columns: the rows of
        the "Columns" sheet."""
        return tuple(
            f"{family.value}{number}"
            for family in FAMILY_ORDER
            for number in range(1, self.counts[family] + 1)
        )


def _numbered(positions: Iterable[Position]) -> list[tuple[Position, int]]:
    """Each position with its 0-based number inside its family."""
    numbers = {family: 0 for family in FAMILY_ORDER}
    out: list[tuple[Position, int]] = []
    for position in positions:
        out.append((position, numbers[position.family]))
        numbers[position.family] += 1
    return out


# ---------------------------------------------------------------------------
# Legend
# ---------------------------------------------------------------------------


def _matrix_group_label(page_object: PageObject, group: int) -> str:
    config = page_object.matrix_config
    defs = config.rows if is_row_oriented(config.selection_rule) else config.cols
    labels = {d.order_index: d.label for d in defs}
    return labels.get(group) or str(group + 1)


def describe(position: Position) -> str:
    """What a position is, for the "Columns" sheet: ``p. 2 · <text>``.

    The question text for a QUESTION; the object's label (or "Matrix") and
    the group's label for a MATRIX; the label, else the placeholder, for a
    free text; the label for a code.
    """
    page_object = position.page_object
    kind = page_object.object_type
    if kind is PageObjectType.QUESTION:
        text = page_object.question_config.question_text or page_object.label or "Question"
    elif kind is PageObjectType.MATRIX:
        text = (
            f"{page_object.label or 'Matrix'} · "
            f"{_matrix_group_label(page_object, position.group)}"
        )
    elif kind is PageObjectType.FREE_TEXT_AREA:
        config = page_object.free_text_config
        text = page_object.label or (config.placeholder if config else None) or "Free text"
    else:
        text = page_object.label or "Code"
    return f"p. {position.page_index} · {text}"


def legend_column(layout: Layout, positions: Sequence[Position]) -> tuple[str | None, ...]:
    """One variant's column of the "Columns" sheet, aligned with
    :meth:`Layout.names`: ``None`` where the variant has no such position."""
    cells: list[str | None] = [None] * sum(layout.counts.values())
    starts = {}
    start = 0
    for family in FAMILY_ORDER:
        starts[family] = start
        start += layout.counts[family]
    for position, number in _numbered(positions):
        cells[starts[position.family] + number] = describe(position)
    return tuple(cells)


# ---------------------------------------------------------------------------
# Cells of one submission
# ---------------------------------------------------------------------------


def _join(items: Iterable[str]) -> str:
    return ", ".join(items)


def _question_cells(
    result: SubmissionObjectResult, page_object: PageObject
) -> tuple[Cell, Cell]:
    options = page_object.question_config.options
    scored = scoring_rules.score_question_result(result, options)
    if scored.unanswered is not None:
        return NO_ANSWER, scored.score
    reading = scoring_rules.question_reading(result)
    if reading.validity is Validity.NO_ANSWER or not reading.selected_option_ids:
        return NO_ANSWER, scored.score
    position = {
        option.id: index
        for index, option in enumerate(sorted(options, key=lambda o: o.order_index))
    }
    # Unknown ids never get here: score_question_result refuses them.
    indices = sorted(position[option_id] for option_id in reading.selected_option_ids)
    return _join(option_letter(index) for index in indices), scored.score


def _group_answer(
    page_object: PageObject, reading: scoring_rules.MatrixGroupReading | None
) -> str:
    if reading is None or reading.validity is Validity.NO_ANSWER or not reading.selected:
        return NO_ANSWER
    config = page_object.matrix_config
    # Inside a row the marks are columns, inside a column they are rows.
    defs = config.cols if is_row_oriented(config.selection_rule) else config.rows
    labels = {d.order_index: d.label for d in defs}
    return _join(labels.get(index) or str(index + 1) for index in sorted(reading.selected))


def _matrix_reading(
    result: SubmissionObjectResult, page_object: PageObject
) -> dict[int, scoring_rules.MatrixGroupReading] | None:
    """The groups of a MATRIX reading by index, ``None`` when unanswered."""
    if scoring_rules.unanswered(result) is not None:
        return None
    reading = scoring_rules.matrix_reading(result)
    if reading is None:
        raise ValueError("a placeholder with no reading is neither flagged nor pending")
    if reading.groups and reading.row_oriented != is_row_oriented(
        page_object.matrix_config.selection_rule
    ):
        raise ValueError("the reading is not oriented as the matrix rule")
    return {group.index: group for group in reading.groups}


def _evaluative_matrix_cells(
    result: SubmissionObjectResult, page_object: PageObject
) -> dict[int, tuple[Cell, Cell]]:
    """Answer and points of every group of an evaluative MATRIX."""
    config = page_object.matrix_config
    scored = scoring_rules.score_matrix_result(
        result, config.rows_count, config.cols_count, config.cells
    )
    groups = _matrix_reading(result, page_object)
    if scored.unanswered is not None or groups is None:
        # A FLAG (or pending) zeroes the whole object.
        return {group: (NO_ANSWER, 0.0) for group in range(_matrix_groups(page_object))}
    points = {group.index: group.score for group in scored.detail.groups}
    return {
        group: (_group_answer(page_object, groups.get(group)), points.get(group, 0.0))
        for group in range(_matrix_groups(page_object))
    }


def _plain_matrix_cells(
    result: SubmissionObjectResult, page_object: PageObject
) -> dict[int, tuple[Cell, Cell]]:
    """Answer of every group of a MATRIX that is not evaluative (no points)."""
    groups = _matrix_reading(result, page_object)
    return {
        group: (
            NO_ANSWER if groups is None else _group_answer(page_object, groups.get(group)),
            None,
        )
        for group in range(_matrix_groups(page_object))
    }


def _verified(value: str | None) -> str:
    return value if value is not None and value.strip() else NO_ANSWER


def _free_text_cell(result: SubmissionObjectResult) -> str:
    if scoring_rules.unanswered(result) is not None:
        return NO_ANSWER
    if result.review_outcome is ReviewOutcome.SUBSTITUTED:
        return _verified(FreeTextReviewedPayload.model_validate(result.reviewed_payload_json).text)
    if result.review_outcome is ReviewOutcome.CONFIRMED and result.detected_payload_json is not None:
        return _verified(
            FreeTextDetectionPayload.model_validate(result.detected_payload_json).suggested_text
        )
    # The OCR text is a suggestion; never exported unconfirmed.
    return NO_ANSWER


def _code_cell(result: SubmissionObjectResult) -> str:
    if scoring_rules.unanswered(result) is not None:
        return NO_ANSWER
    if result.review_outcome is ReviewOutcome.SUBSTITUTED:
        return _verified(CodeReviewedPayload.model_validate(result.reviewed_payload_json).value)
    if result.detected_payload_json is None:
        return NO_ANSWER
    detected = CodeDetectionPayload.model_validate(result.detected_payload_json)
    return _verified(detected.decoded_value if detected.decode_successful else None)


def _object_cells(
    family: Family, result: SubmissionObjectResult | None, page_object: PageObject
) -> dict[int | None, tuple[Cell, Cell]]:
    """Answer and points of each position of one object, keyed by group
    (``None`` for an object that is not a MATRIX)."""
    if page_object.object_type is PageObjectType.MATRIX:
        if result is None:
            # No reading (missing page): "no answer", worth 0 if it counts.
            points = 0.0 if family is Family.D else None
            return {
                group: (NO_ANSWER, points) for group in range(_matrix_groups(page_object))
            }
        if family is Family.D:
            return _evaluative_matrix_cells(result, page_object)
        return _plain_matrix_cells(result, page_object)
    if result is None:
        return {None: (NO_ANSWER, 0.0 if family is Family.D else None)}
    if family is Family.D:
        return {None: _question_cells(result, page_object)}
    if family is Family.T:
        return {None: (_free_text_cell(result), None)}
    return {None: (_code_cell(result), None)}


def submission_cells(
    layout: Layout,
    positions: Sequence[Position],
    page_objects: Iterable[PageObject],
    results: Iterable[SubmissionObjectResult],
) -> tuple[Cell, ...]:
    """The columns per question of one closed submission, aligned with
    :meth:`Layout.headers`.

    *positions* are its variant's (:func:`variant_positions`), *page_objects*
    every exportable object of that variant (for the first-reading rule),
    *results* the submission's results, each with its ``submission_page``.
    Columns beyond the variant's positions stay empty.
    """
    by_id = {page_object.id: page_object for page_object in page_objects}
    first = scoring_rules.first_readings(by_id, results)

    cells: list[Cell] = [None] * layout.width
    computed: dict[uuid.UUID, dict[int | None, tuple[Cell, Cell]] | None] = {}
    for position, number in _numbered(positions):
        page_object = position.page_object
        key = scoring_rules.reading_key(page_object)
        if key not in computed:
            # The reading is scored against the copy it was READ ON, as
            # ``submission_score`` does: linked copies have options of their
            # own (other ids), so the first copy in layout order would not
            # recognise a reading made on page 2.
            result, read_on = first.get(key, (None, page_object))
            try:
                computed[key] = _object_cells(position.family, result, read_on)
            except ValueError:
                logger.exception(
                    "Cannot export result %s of page object %s",
                    result.id if result is not None else None,
                    read_on.id,
                )
                computed[key] = None
        object_cells = computed[key]
        if object_cells is None or position.group not in object_cells:
            # Broken data only: a copy whose grid lags behind the first one.
            continue
        answer, points = object_cells[position.group]
        if position.family is Family.D:
            start = layout.offset(Family.D) + 2 * number
            cells[start] = answer
            cells[start + 1] = points
        else:
            cells[layout.offset(position.family) + number] = answer
    return tuple(cells)
