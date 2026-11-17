# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Batch results export: the Summary export and the Detail export.

One row per submission of a batch that is ready for export
(``submission_batch_service.require_ready_for_export``), built here once and
written as .xlsx or .csv by ``export_writers``. Read-only, nothing persisted.

What a row holds:

* ``Submission``: the short id the queue shows (``abcd…wxyz``), so two
  submissions without a candidate can be told apart and found again.
* ``Candidate``: the raw identifier, empty if none; ``Variant``: its name.
* ``Points``: ``Submission.total_score``, written by the close.
  ``Max points``: ``scoring_rules.variant_max_score`` of the variant, computed
  ONCE per variant. ``Percentage``: ``scoring_rules.percentage``, the same
  formula as the review page. Only a closed submission (REVIEWED, FINALIZED)
  has a score; in a ready batch the only other status left is UNRESOLVABLE,
  whose three cells stay empty. A closed submission with ``total_score`` NULL
  (score not computable at the close) has empty points and percentage, and
  its maximum.
* ``Status``: the status code. ``Missing pages``: as in the queue.
* ``Unreviewed objects``: the results left unresolved at the close — the
  "N unresolved" of the queue on a closed submission, 0 when the grade is
  fully verified. Empty on an UNRESOLVABLE one: 0 would read as "verified".
* ``Batch``: the batch name; ``Scanned at``: ``Submission.created_at``, the
  upload of the submission's first sheet (the scan date is not in the files).

Rows in upload order, as the queue.

Gate and rows from ONE snapshot: :func:`begin_read_snapshot`
turns the request's transaction into a ``REPEATABLE READ, READ ONLY`` one
before the gate reads, so an "Undo unresolvable" or a reopening committed
meanwhile cannot slip a batch that is no longer ready into the file. The
endpoint calls it; :func:`export_summary` itself does not, so it runs inside
the integration tests' outer transaction too.

A constant number of queries, plus one set per distinct variant for its
maximum (``page_object_repo.list_scorable_for_variant``).

The Detail export is the Summary export rows, unchanged, plus the columns per
question of ``export_detail``: for the variants of the CLOSED submissions,
their exportable objects in one load, and the results of every closed
submission in one more — still a constant number of queries. An UNRESOLVABLE
submission has no column per question filled, and a variant only UNRESOLVABLE
submissions hold adds no column. ``Dn_pt`` are recomputed from the results
while ``Points`` is the ``total_score`` saved at the close: they agree, since
a published variant's options do not change and a closed submission is not
touched.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.enums import CLOSED_SUBMISSION_STATUSES
from app.models.submission import Submission
from app.models.submission_batch import SubmissionBatch
from app.models.user import User
from app.repositories import (
    form_variant_page_repo,
    form_variant_repo,
    page_object_repo,
    submission_batch_repo,
    submission_object_result_repo,
)
from app.services import export_detail, scoring_rules, submission_batch_service
from app.services.export_writers import DetailRow, DetailTable, SummaryRow


def begin_read_snapshot(db: Session) -> None:
    """End the request's transaction so far (the authentication read) and open
    a ``REPEATABLE READ, READ ONLY`` one: every later read sees the same
    snapshot of the database.

    ``SET TRANSACTION`` must be the first statement of its transaction, hence
    the commit right before it, with nothing read in between. Read-only: the
    export writes nothing, and the database refuses it if it ever tried.
    """
    db.commit()
    db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))


def short_submission_id(submission_id: uuid.UUID) -> str:
    """``abcd…wxyz``: the id as the queue shows it (``truncateSubmissionId``
    in the FE)."""
    full = str(submission_id)
    return f"{full[:4]}…{full[-4:]}" if len(full) > 9 else full


@dataclass(frozen=True)
class SummaryExport:
    batch: SubmissionBatch
    rows: list[SummaryRow]


def export_summary(db: Session, batch_id: uuid.UUID, user: User) -> SummaryExport:
    """The Summary export rows of *batch_id*, if it can be exported now.

    404 / 403 as every batch read; the structured 409 ``NOT_EXPORT_READY`` of
    the gate otherwise. Any project member, the ACTIVE gate not
    applied: results are exported after collection has closed.
    """
    batch = submission_batch_service.require_ready_for_export(db, batch_id, user)
    return SummaryExport(batch=batch, rows=summary_rows(db, batch))


@dataclass(frozen=True)
class DetailExport:
    batch: SubmissionBatch
    table: DetailTable


def export_detail_table(db: Session, batch_id: uuid.UUID, user: User) -> DetailExport:
    """The Detail export of *batch_id*, if it can be exported now: same gate,
    errors and authorization as :func:`export_summary`."""
    batch = submission_batch_service.require_ready_for_export(db, batch_id, user)
    return DetailExport(batch=batch, table=detail_table(db, batch))


def summary_rows(db: Session, batch: SubmissionBatch) -> list[SummaryRow]:
    """One :class:`SummaryRow` per submission of *batch*, in upload order
    (module docstring)."""
    submissions = submission_batch_repo.list_submissions(db, batch.id)
    return _summary_rows(db, batch, submissions, _variant_names(db, batch))


def _variant_names(db: Session, batch: SubmissionBatch) -> dict[uuid.UUID, str]:
    return {
        variant.id: variant.name
        for variant in form_variant_repo.list_for_project(db, batch.form_project_id)
    }


def detail_table(db: Session, batch: SubmissionBatch) -> DetailTable:
    """The Detail export of *batch*: the Summary export rows with the columns
    per question, and the legend of the "Columns" sheet (module docstring)."""
    submissions = submission_batch_repo.list_submissions(db, batch.id)
    variant_names = _variant_names(db, batch)
    summaries = _summary_rows(db, batch, submissions, variant_names)

    closed_variant_ids = {
        s.matched_form_variant_id
        for s in submissions
        if s.status in CLOSED_SUBMISSION_STATUSES and s.matched_form_variant_id is not None
    }
    objects_by_variant: dict[uuid.UUID, list] = defaultdict(list)
    for page_object in page_object_repo.list_exportable_for_variants(db, closed_variant_ids):
        objects_by_variant[page_object.form_variant_page.form_variant_id].append(page_object)
    positions = {
        variant_id: export_detail.variant_positions(objects_by_variant[variant_id])
        for variant_id in closed_variant_ids
    }
    layout = export_detail.Layout.of(positions.values())

    results_by_submission: dict[uuid.UUID, list] = defaultdict(list)
    for result in submission_object_result_repo.list_closed_results_for_batch(db, batch.id):
        results_by_submission[result.submission_page.submission_id].append(result)

    rows = []
    for submission, summary in zip(submissions, summaries, strict=True):
        variant_id = submission.matched_form_variant_id
        if submission.status in CLOSED_SUBMISSION_STATUSES and variant_id is not None:
            cells = export_detail.submission_cells(
                layout,
                positions[variant_id],
                objects_by_variant[variant_id],
                results_by_submission[submission.id],
            )
        else:
            cells = (None,) * layout.width
        rows.append(DetailRow(summary=summary, cells=cells))

    # The legend: the variants that have columns, by name.
    legend_ids = sorted(
        closed_variant_ids, key=lambda vid: (variant_names.get(vid, ""), str(vid))
    )
    columns = [export_detail.legend_column(layout, positions[vid]) for vid in legend_ids]
    legend = [
        (name, tuple(column[index] for column in columns))
        for index, name in enumerate(layout.names())
    ]
    return DetailTable(
        headers=layout.headers(),
        rows=rows,
        legend_variants=tuple(variant_names.get(vid, "") for vid in legend_ids),
        legend=legend,
    )


def _summary_rows(
    db: Session,
    batch: SubmissionBatch,
    submissions: list[Submission],
    variant_names: dict[uuid.UUID, str],
) -> list[SummaryRow]:
    pending = submission_object_result_repo.pending_review_counts_for_batch(
        db, batch.id
    )
    variant_ids = {
        s.matched_form_variant_id
        for s in submissions
        if s.matched_form_variant_id is not None
    }
    present_pages = submission_batch_repo.present_page_indices_for_batch(db, batch.id)
    expected_pages = form_variant_page_repo.page_indices_for_variants(db, variant_ids)
    # Only the variants a closed submission needs a maximum for.
    max_scores = {
        variant_id: scoring_rules.variant_max_score(
            page_object_repo.list_scorable_for_variant(db, variant_id)
        )
        for variant_id in {
            s.matched_form_variant_id
            for s in submissions
            if s.status in CLOSED_SUBMISSION_STATUSES
            and s.matched_form_variant_id is not None
        }
    }

    return [
        _row(
            submission,
            batch,
            variant_names=variant_names,
            max_scores=max_scores,
            pending=pending,
            missing_pages=submission_batch_service._missing_page_indices(
                submission, expected_pages, present_pages
            ),
        )
        for submission in submissions
    ]


def _row(
    submission: Submission,
    batch: SubmissionBatch,
    *,
    variant_names: dict[uuid.UUID, str],
    max_scores: dict[uuid.UUID, float],
    pending: dict[uuid.UUID, int],
    missing_pages: list[int],
) -> SummaryRow:
    variant_id = submission.matched_form_variant_id
    closed = submission.status in CLOSED_SUBMISSION_STATUSES

    points = submission.total_score if closed else None
    max_points = max_scores.get(variant_id) if closed and variant_id else None
    percentage = (
        scoring_rules.percentage(points, max_points)
        if points is not None and max_points is not None
        else None
    )

    return SummaryRow(
        submission=short_submission_id(submission.id),
        candidate=submission.respondent_identifier,
        variant=variant_names.get(variant_id) if variant_id else None,
        points=points,
        max_points=max_points,
        percentage=percentage,
        status=submission.status.value,
        missing_pages=tuple(missing_pages),
        unreviewed_objects=pending.get(submission.id, 0) if closed else None,
        batch=batch.name,
        scanned_at=submission.created_at,
    )
