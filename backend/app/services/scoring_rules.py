# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""How much a reading is worth.

The ONE place the scoring rules live, as ``review_rules`` is for the review:
the "no answer" states, the maximum score, the submission total and the live
score all build on these functions instead of re-deriving them.

Pure: they read the authored options and the reading, nothing else — no
session, no query, no write. Nothing is persisted either
(``SubmissionObjectResult.auto_score`` / ``final_score`` stay unused): the
score is an aggregate computed on the fly, and the options of a published
variant cannot change under it, since objects are editable only in DRAFT.

The algorithm
-------------
``raw_score`` is the sum of the scores of the selected options. An option with
no score configured is worth 0, even when it is marked ``is_correct``: that
flag does not take part in scoring. Then, in this order:

  * FULL_ANSWER — every option of the question selected, on a question with at
    least two options — scores 0 whatever the ``raw_score`` and the validity.
    It is a rule of SCORING, not a ``Validity``: extraction and review leave
    such a reading TOO_MANY_ANSWERS (single-choice) or VALID (multi-choice),
    and this module recognises it. Checked first, so a single-choice question
    with every option marked scores 0 even where ``min(0, raw_score)`` would
    be negative;
  * NO_ANSWER scores 0;
  * VALID scores ``raw_score``;
  * TOO_MANY_ANSWERS scores ``min(0, raw_score)`` — never positive.

A non-univocal answer never earns positive points.

MATRIX
------
Only an evaluative MATRIX without the RESPONDENT_IDENTIFIER role is scored
(:func:`is_scored_matrix`). Each GROUP is a mini-QUESTION scored by the same
table: the row under a row rule (ONE_PER_ROW, MANY_PER_ROW, FREE), the column
under a column rule — the group that carries the validity in extraction and
review. The options of a group are ALL its printed bubbles,
``range(cols_count)`` for a row and ``range(rows_count)`` for a column, not
only the cells authored with data: a cell with no ``MatrixCellDef`` or no
score is worth 0, and FULL_ANSWER means every printed bubble of the group. The
object's score is the algebraic sum of its groups'.

Both shapes go through ONE private core, :func:`_score_selection`, keyed by
whatever identifies an option: an option id for a QUESTION, a column (or row)
index inside a MATRIX group.

Which reading
-------------
:func:`question_reading` says what is scored: the operator's value for a
SUBSTITUTED result, the extracted reading otherwise, nothing for a placeholder
("Review anyway") no one substituted. It does NOT apply the "no answer" rules:
a FLAGGED result, or one still pending, returns its reading here, and the
"No answer" section below decides that it scores 0. Which reading counts
when a submission holds several of the same object (duplicate sheets) is the
submission total's business: the first in physical page order.

No answer
---------
Three cases leave an object with no determined answer, and all three score 0
and stay in the total:

  * the candidate did not answer — a reading whose validity is NO_ANSWER, a
    SUBSTITUTED one with no option included: the table above already gives 0;
  * the operator FLAGGED it — :attr:`Unanswered.FLAGGED`;
  * it is still pending — ``review_required`` and not resolved —
    :attr:`Unanswered.UNRESOLVED`, in ANY status of the submission: the
    live score of an open submission is what closing it now would save. A
    reading that never needed review is not pending and scores its reading.

:func:`score_question_result` and :func:`score_matrix_result` are what the
submission total and the export call: they apply the two ``Unanswered`` cases
before reading the payload, so a FLAG zeroes a whole MATRIX, and keep the
case on the result so review and export can still tell them apart. A
placeholder is born ``review_required``, so one with no reading is always
FLAGGED or pending; anything else is a broken invariant.

Maximum score
-------------
The maximum of an object is what the best possible sheet actually scores under
the table above, so 100% is always reachable. Per mini-QUESTION, with every
option's score (``None`` = 0):

  * single selection (SINGLE_CHOICE, a ONE_PER_ROW / ONE_PER_COLUMN group):
    the highest option score, never below 0 — leaving it blank scores 0;
  * multiple selection (MULTIPLE_CHOICE, MANY_PER_ROW / MANY_PER_COLUMN,
    FREE): the sum of the positive scores, minus the lowest of them when every
    option is positive and there are at least two — marking them all is
    FULL_ANSWER, worth 0.

A MATRIX adds up its groups, oriented as its rule says. Which objects count is
:func:`variant_max_score`'s business: QUESTIONs and :func:`is_scored_matrix`
MATRIXes, a linked group ("shared across pages") ONCE — its copies are one
question printed again, and the submission total scores only the first
reading in physical page order, as for duplicate sheets. A detached copy is
an object of its own. The submission total reuses this function for its
max_possible_score.

The submission score
--------------------
:func:`submission_score` is the triple: points earned, the variant's maximum,
and the percentage. The objects that count are the maximum's (:func:`_counts`),
and each counts ONCE: the readings of one object — duplicate sheets of a page,
or the linked copies of a group — are scored by the FIRST in physical page
order, ``(source_page_order, id)`` as for the candidate
(``review_rules.first_identifier_reading``), even when that one is FLAGGED or
pending and a later one is answered. An object with no reading at all (its
page is missing) is worth 0 and the maximum does not change. The percentage
is ``None`` when the maximum is 0, and a negative total stays negative.
"""
from __future__ import annotations

import enum
import uuid
from collections.abc import Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TypeVar

from app.models.enums import (
    MatrixSelectionRule,
    PageObjectRole,
    PageObjectType,
    QuestionType,
    ReviewOutcome,
    SubmissionResultType,
    Validity,
)
from app.models.matrix_cell_def import MatrixCellDef
from app.models.page_object import PageObject
from app.models.question_option import QuestionOption
from app.models.submission_object_result import SubmissionObjectResult
from app.schemas.submission_result import (
    MatrixDetectionPayload,
    MatrixReviewedPayload,
    QuestionDetectionPayload,
    QuestionReviewedPayload,
)
from app.services.matrix_selection import is_row_oriented, is_single_selection

K = TypeVar("K", bound=Hashable)


@dataclass(frozen=True)
class QuestionReading:
    """What a QUESTION result says was marked, and whether that is valid."""

    selected_option_ids: frozenset[uuid.UUID]
    validity: Validity


@dataclass(frozen=True)
class QuestionScore:
    """The score of one QUESTION reading.

    ``raw_score`` is the plain sum of the selected options; ``score`` is what
    the reading is worth once the validity and FULL_ANSWER rules are applied;
    ``full_answer`` says FULL_ANSWER applied, so a later reader (review,
    export) does not have to work it out again.
    """

    raw_score: float
    score: float
    full_answer: bool


@dataclass(frozen=True)
class MatrixGroupReading:
    """What one group (row or column) of a MATRIX result says was marked.

    ``selected`` holds column indices inside a row, row indices inside a
    column.
    """

    index: int
    selected: frozenset[int]
    validity: Validity


@dataclass(frozen=True)
class MatrixReading:
    """The groups of a MATRIX result: rows if ``row_oriented``, else columns."""

    row_oriented: bool
    groups: tuple[MatrixGroupReading, ...]


@dataclass(frozen=True)
class MatrixGroupScore:
    """The score of one group, as :class:`QuestionScore` is for a QUESTION."""

    index: int
    raw_score: float
    score: float
    full_answer: bool


@dataclass(frozen=True)
class MatrixScore:
    """The score of a MATRIX reading: the sum of its groups', and each group."""

    score: float
    groups: tuple[MatrixGroupScore, ...]


def _score_selection(
    scores: Mapping[K, float | None],
    selected: Iterable[K],
    validity: Validity,
) -> QuestionScore:
    """The scoring table on one mini-QUESTION: ``scores`` maps EVERY option of
    it to its configured score (``None`` = not configured, worth 0)."""
    selected = frozenset(selected)
    unknown = selected - scores.keys()
    if unknown:
        raise ValueError(
            f"selected options not in the question: {sorted(map(str, unknown))}"
        )

    raw_score = sum((scores[option] or 0.0 for option in selected), 0.0)
    full_answer = len(scores) >= 2 and selected == scores.keys()

    if full_answer or validity is Validity.NO_ANSWER:
        score = 0.0
    elif validity is Validity.VALID:
        score = raw_score
    elif validity is Validity.TOO_MANY_ANSWERS:
        score = min(0.0, raw_score)
    else:
        # Not a ``Validity`` member (a raw string read from a payload, say):
        # scored by elimination it would silently take the TOO_MANY_ANSWERS
        # branch, so it is refused like any other broken invariant.
        raise ValueError(f"not a Validity: {validity!r}")
    return QuestionScore(raw_score=raw_score, score=score, full_answer=full_answer)


def score_question(
    options: Sequence[QuestionOption],
    selected_option_ids: Iterable[uuid.UUID],
    validity: Validity,
) -> QuestionScore:
    """Score one QUESTION reading against its authored ``options``.

    Raises ``ValueError`` if a selected id is not one of ``options``: the
    payloads are validated by the server and a published variant's options do
    not change, so that is a broken invariant, better surfaced than scored.
    """
    return _score_selection(
        {option.id: option.score for option in options}, selected_option_ids, validity
    )


def is_scored_matrix(page_object: PageObject) -> bool:
    """Whether *page_object* is a MATRIX that counts for the score.

    Evaluative in authoring and not a respondent-ID grid: a
    RESPONDENT_IDENTIFIER role, own or of the linked group
    (``effective_role``), takes it out even when the flag is set. For the
    object selection of the maximum score, the submission total and the
    Detail export; :func:`score_matrix` does not check it.
    """
    return (
        page_object.object_type is PageObjectType.MATRIX
        and page_object.matrix_config is not None
        and page_object.matrix_config.evaluative
        and page_object.effective_role is not PageObjectRole.RESPONDENT_IDENTIFIER
    )


def score_matrix(
    rows_count: int,
    cols_count: int,
    cells: Sequence[MatrixCellDef],
    reading: MatrixReading,
) -> MatrixScore:
    """Score one MATRIX reading against its grid and authored ``cells``.

    Raises ``ValueError`` on a group or a selected index outside the grid, and
    on a group listed twice (it would be counted twice): broken invariants, as
    for :func:`score_question`. A group missing from the reading contributes
    nothing; the payloads list every group anyway.
    """
    score_by_cell = {(cell.row_index, cell.col_index): cell.score for cell in cells}
    if reading.row_oriented:
        group_kind, groups_count, options_count = "row", rows_count, cols_count
    else:
        group_kind, groups_count, options_count = "column", cols_count, rows_count

    seen: set[int] = set()
    groups: list[MatrixGroupScore] = []
    for group in reading.groups:
        if not 0 <= group.index < groups_count:
            raise ValueError(
                f"{group_kind} {group.index} outside a {rows_count}x{cols_count} grid"
            )
        if group.index in seen:
            raise ValueError(f"{group_kind} {group.index} listed twice")
        seen.add(group.index)
        outside = sorted(i for i in group.selected if not 0 <= i < options_count)
        if outside:
            raise ValueError(
                f"{group_kind} {group.index}: selected {outside} outside a "
                f"{rows_count}x{cols_count} grid"
            )

        scores = {
            option: score_by_cell.get(
                (group.index, option) if reading.row_oriented else (option, group.index)
            )
            for option in range(options_count)
        }
        scored = _score_selection(scores, group.selected, group.validity)
        groups.append(
            MatrixGroupScore(
                index=group.index,
                raw_score=scored.raw_score,
                score=scored.score,
                full_answer=scored.full_answer,
            )
        )
    return MatrixScore(
        score=sum((group.score for group in groups), 0.0), groups=tuple(groups)
    )


def question_reading(result: SubmissionObjectResult) -> QuestionReading | None:
    """The reading of a QUESTION result to score (see the module docstring).

    ``None`` for a placeholder no one substituted: nothing was read.
    """
    if result.result_type is not SubmissionResultType.QUESTION:
        raise ValueError(f"not a QUESTION result: {result.result_type}")

    if result.review_outcome is ReviewOutcome.SUBSTITUTED:
        reviewed = QuestionReviewedPayload.model_validate(result.reviewed_payload_json)
        return QuestionReading(
            selected_option_ids=frozenset(reviewed.selected_option_ids),
            validity=reviewed.validity,
        )
    if result.detected_payload_json is None:
        return None
    detected = QuestionDetectionPayload.model_validate(result.detected_payload_json)
    return QuestionReading(
        selected_option_ids=frozenset(
            option.option_id for option in detected.selected_options
        ),
        validity=detected.validity,
    )


def matrix_reading(result: SubmissionObjectResult) -> MatrixReading | None:
    """The reading of a MATRIX result to score, chosen as in
    :func:`question_reading`.

    ``None`` for a placeholder no one substituted. A respondent-ID grid is a
    RESPONDENT_IDENTIFIER result, not a MATRIX one, and is refused like any
    other type.
    """
    if result.result_type is not SubmissionResultType.MATRIX:
        raise ValueError(f"not a MATRIX result: {result.result_type}")

    if result.review_outcome is ReviewOutcome.SUBSTITUTED:
        reviewed = MatrixReviewedPayload.model_validate(result.reviewed_payload_json)
        return _matrix_reading(
            rows=[
                MatrixGroupReading(
                    index=row.row_index,
                    selected=frozenset(row.selected_col_indices),
                    validity=row.validity,
                )
                for row in reviewed.rows
            ],
            cols=[
                MatrixGroupReading(
                    index=col.col_index,
                    selected=frozenset(col.selected_row_indices),
                    validity=col.validity,
                )
                for col in reviewed.cols
            ],
        )
    if result.detected_payload_json is None:
        return None
    detected = MatrixDetectionPayload.model_validate(result.detected_payload_json)
    return _matrix_reading(
        rows=[
            MatrixGroupReading(
                index=row.row_index,
                selected=frozenset(cell.col_index for cell in row.selected_cells),
                validity=row.validity,
            )
            for row in detected.rows
        ],
        cols=[
            MatrixGroupReading(
                index=col.col_index,
                selected=frozenset(cell.row_index for cell in col.selected_cells),
                validity=col.validity,
            )
            for col in detected.cols
        ],
    )


def _matrix_reading(
    rows: list[MatrixGroupReading], cols: list[MatrixGroupReading]
) -> MatrixReading:
    """Rows or columns, never both (``MatrixDetectionPayload``). A degenerate
    matrix has neither and reads as row-oriented with no group: it scores 0
    either way."""
    if rows and cols:
        raise ValueError("a MATRIX reading has both rows and columns")
    if cols:
        return MatrixReading(row_oriented=False, groups=tuple(cols))
    return MatrixReading(row_oriented=True, groups=tuple(rows))


class Unanswered(str, enum.Enum):
    """Why a result scores 0 whatever its reading (see the module docstring).

    Not persisted: computed from the result each time, like the score.
    """

    FLAGGED = "FLAGGED"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True)
class QuestionResultScore:
    """The score of one QUESTION result.

    ``detail`` is the scored reading, ``None`` when ``unanswered`` says why
    the result was not read at all.
    """

    score: float
    unanswered: Unanswered | None
    detail: QuestionScore | None


@dataclass(frozen=True)
class MatrixResultScore:
    """The score of one MATRIX result, as :class:`QuestionResultScore`."""

    score: float
    unanswered: Unanswered | None
    detail: MatrixScore | None


def unanswered(result: SubmissionObjectResult) -> Unanswered | None:
    """Whether *result* scores 0 as FLAGGED or still pending, or ``None``
    if its reading is scored.

    Pending is the definition of the review counts
    (``count_pending_review_for_submission``): ``review_required`` and
    ``review_outcome`` NULL, whatever the status of the submission.
    """
    if result.review_outcome is ReviewOutcome.FLAGGED:
        return Unanswered.FLAGGED
    if result.review_required and result.review_outcome is None:
        return Unanswered.UNRESOLVED
    return None


def score_question_result(
    result: SubmissionObjectResult, options: Sequence[QuestionOption]
) -> QuestionResultScore:
    """Score one QUESTION result against its authored ``options``, the
    "no answer" cases included.

    Raises ``ValueError`` on a result that is not a QUESTION, and on a
    placeholder with no reading that is neither FLAGGED nor pending.
    """
    if result.result_type is not SubmissionResultType.QUESTION:
        raise ValueError(f"not a QUESTION result: {result.result_type}")

    case = unanswered(result)
    if case is not None:
        return QuestionResultScore(score=0.0, unanswered=case, detail=None)
    reading = question_reading(result)
    if reading is None:
        raise ValueError("a placeholder with no reading is neither flagged nor pending")
    scored = score_question(options, reading.selected_option_ids, reading.validity)
    return QuestionResultScore(score=scored.score, unanswered=None, detail=scored)


def score_matrix_result(
    result: SubmissionObjectResult,
    rows_count: int,
    cols_count: int,
    cells: Sequence[MatrixCellDef],
) -> MatrixResultScore:
    """Score one MATRIX result against its grid and authored ``cells``, the
    "no answer" cases included: a FLAG zeroes the whole object.

    Raises ``ValueError`` as :func:`score_question_result` does, and as
    :func:`score_matrix` does on a reading outside the grid.
    """
    if result.result_type is not SubmissionResultType.MATRIX:
        raise ValueError(f"not a MATRIX result: {result.result_type}")

    case = unanswered(result)
    if case is not None:
        return MatrixResultScore(score=0.0, unanswered=case, detail=None)
    reading = matrix_reading(result)
    if reading is None:
        raise ValueError("a placeholder with no reading is neither flagged nor pending")
    scored = score_matrix(rows_count, cols_count, cells, reading)
    return MatrixResultScore(score=scored.score, unanswered=None, detail=scored)


def _max_selection(scores: Mapping[K, float | None], single: bool) -> float:
    """The best score one mini-QUESTION can earn: the maximum of
    :func:`_score_selection` over every possible selection.

    ``scores`` maps EVERY option to its configured score, as there; ``single``
    says more than one option is TOO_MANY_ANSWERS.
    """
    values = [score or 0.0 for score in scores.values()]
    if single:
        return max((0.0, *values))
    positives = sorted(value for value in values if value > 0)
    best = sum(positives, 0.0)
    if len(values) >= 2 and len(positives) == len(values):
        # Every option positive: all of them is FULL_ANSWER, so the best
        # selection leaves out the cheapest.
        best -= positives[0]
    return best


def question_max_score(
    question_type: QuestionType, options: Sequence[QuestionOption]
) -> float:
    """The maximum score of a QUESTION with these authored ``options``."""
    return _max_selection(
        {option.id: option.score for option in options},
        single=question_type is QuestionType.SINGLE_CHOICE,
    )


def matrix_max_score(
    selection_rule: MatrixSelectionRule,
    rows_count: int,
    cols_count: int,
    cells: Sequence[MatrixCellDef],
) -> float:
    """The maximum score of a MATRIX: the sum of its groups' maxima.

    The group is the row or the column as *selection_rule* orients it, the
    same group :func:`score_matrix` scores; its options are all the printed
    bubbles, a cell with no ``MatrixCellDef`` or no score worth 0. A cell
    outside the grid is not a printed bubble and is ignored.
    """
    score_by_cell = {(cell.row_index, cell.col_index): cell.score for cell in cells}
    row_oriented = is_row_oriented(selection_rule)
    groups_count, options_count = (
        (rows_count, cols_count) if row_oriented else (cols_count, rows_count)
    )
    single = is_single_selection(selection_rule)
    return sum(
        (
            _max_selection(
                {
                    option: score_by_cell.get(
                        (group, option) if row_oriented else (option, group)
                    )
                    for option in range(options_count)
                },
                single=single,
            )
            for group in range(groups_count)
        ),
        0.0,
    )


def _counts(page_object: PageObject) -> bool:
    """Whether *page_object* counts for the score, in the maximum and in the
    submission total alike: a QUESTION with its config, or a
    :func:`is_scored_matrix` MATRIX. A QUESTION without its config has nothing
    to score and counts in neither."""
    if page_object.object_type is PageObjectType.QUESTION:
        return page_object.question_config is not None
    return is_scored_matrix(page_object)


def _is_linked(page_object: PageObject) -> bool:
    return bool(page_object.shared_across_pages) and page_object.shared_group_id is not None


def variant_max_score(page_objects: Iterable[PageObject]) -> float:
    """The maximum score of a FormVariant made of *page_objects*.

    *page_objects* are every object of the variant in physical page order.
    Counted: QUESTIONs and :func:`is_scored_matrix` MATRIXes, the first linked
    copy of a group only; everything else is worth nothing.
    """
    seen_groups: set[uuid.UUID] = set()
    total = 0.0
    for page_object in page_objects:
        if _is_linked(page_object):
            if page_object.shared_group_id in seen_groups:
                continue
            seen_groups.add(page_object.shared_group_id)

        if not _counts(page_object):
            continue
        if page_object.object_type is PageObjectType.QUESTION:
            config = page_object.question_config
            total += question_max_score(config.question_type, config.options)
        else:
            config = page_object.matrix_config
            total += matrix_max_score(
                config.selection_rule, config.rows_count, config.cols_count, config.cells
            )
    return total


@dataclass(frozen=True)
class SubmissionScore:
    """The score of a submission as a triple: points earned, the maximum, and
    the percentage.

    ``percentage`` is ``raw_score / max_possible_score * 100``, ``None`` when
    the maximum is 0.
    """

    raw_score: float
    max_possible_score: float
    percentage: float | None


def _result_score(result: SubmissionObjectResult, page_object: PageObject) -> float:
    if page_object.object_type is PageObjectType.QUESTION:
        return score_question_result(result, page_object.question_config.options).score
    config = page_object.matrix_config
    return score_matrix_result(
        result, config.rows_count, config.cols_count, config.cells
    ).score


def submission_score(
    page_objects: Iterable[PageObject],
    results: Iterable[SubmissionObjectResult],
) -> SubmissionScore:
    """The score of a submission (see the module docstring).

    *page_objects* are every object of the submission's variant in physical page
    order, as for :func:`variant_max_score`; *results* its QUESTION and MATRIX
    results, each with its ``submission_page`` loaded (for the physical order).
    A result whose object does not count, or is not among *page_objects*, is
    ignored: a FLAG on a MATRIX that is not evaluative must not reach the total.

    Raises ``ValueError`` where the per-result rules do (a broken invariant).
    """
    page_objects = list(page_objects)
    counted = {
        page_object.id: page_object
        for page_object in page_objects
        if _counts(page_object)
    }

    raw_score = sum(
        (
            _result_score(result, page_object)
            for result, page_object in first_readings(counted, results).values()
        ),
        0.0,
    )
    max_possible_score = variant_max_score(page_objects)
    return SubmissionScore(
        raw_score=raw_score,
        max_possible_score=max_possible_score,
        percentage=percentage(raw_score, max_possible_score),
    )


def reading_key(page_object: PageObject) -> uuid.UUID:
    """What one object of a submission is, for "which reading counts": the
    linked group for a linked copy (its copies are one question printed
    again), the object itself otherwise — a detached copy included."""
    return page_object.shared_group_id if _is_linked(page_object) else page_object.id


def first_readings(
    page_objects: Mapping[uuid.UUID, PageObject],
    results: Iterable[SubmissionObjectResult],
) -> dict[uuid.UUID, tuple[SubmissionObjectResult, PageObject]]:
    """Per :func:`reading_key`, the FIRST reading of the object in physical
    page order, ``(source_page_order, id)``, and the copy it was read on.

    The one rule for duplicate sheets and linked copies: the submission total
    and the Detail export both read through it. *page_objects* maps the ids of
    the objects to consider; a result of any other object is ignored. Each
    result needs its ``submission_page`` loaded. The first reading wins even
    when it is FLAGGED or pending and a later one is answered.
    """
    first: dict[uuid.UUID, tuple[tuple[int, uuid.UUID], SubmissionObjectResult, PageObject]] = {}
    for result in results:
        page_object = page_objects.get(result.page_object_id)
        if page_object is None:
            continue
        key = reading_key(page_object)
        order = (result.submission_page.source_page_order, result.id)
        if key not in first or order < first[key][0]:
            first[key] = (order, result, page_object)
    return {key: (result, page_object) for key, (_, result, page_object) in first.items()}


def percentage(raw_score: float, max_possible_score: float) -> float | None:
    """``raw_score / max_possible_score * 100``, ``None`` when the maximum is 0;
    a negative total stays negative.

    The one formula of the percentage: the score of a submission in review and
    the batch export both call it.
    """
    if not max_possible_score:
        return None
    return raw_score / max_possible_score * 100
