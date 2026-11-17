# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Data-access layer for SubmissionBatch / Submission.

Scoped to what scan-batch ingestion needs: create a batch, resolve it, and list
its submissions with their page counts for the minimal verification endpoint —
plus a project's batches with their per-status counts for the batch list, the
raw page facts the review queue's "missing pages" column is computed from, the
row-locking read the review soft lock writes through, one submission's sheets
for its review page, the bound sheets the "unread objects" count reads, one
sheet by id for its scanned image, and the submissions a threshold change may
give a candidate to.
"""
from __future__ import annotations

import uuid
from collections.abc import Collection

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.models.enums import SubmissionStatus
from app.models.form_variant_page import FormVariantPage
from app.models.submission import Submission
from app.models.submission_batch import SubmissionBatch
from app.models.submission_page import SubmissionPage


# ---------------------------------------------------------------------------
# SubmissionBatch
# ---------------------------------------------------------------------------


def get(db: Session, batch_id: uuid.UUID) -> SubmissionBatch | None:
    return db.get(SubmissionBatch, batch_id)


def get_for_update(db: Session, batch_id: uuid.UUID) -> SubmissionBatch | None:
    """One batch, its row locked ``FOR UPDATE`` until the transaction ends.

    Reopening a submission reads the batch's threshold through here: a threshold
    change writes this row before it reads the results it recomputes, so the
    two serialise on it — the reopen either waits and sees the new threshold,
    or commits first and is seen by the change. ``populate_existing`` for the
    same reason as :func:`get_submission_for_update`.
    """
    stmt = (
        select(SubmissionBatch)
        .where(SubmissionBatch.id == batch_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return db.scalars(stmt).one_or_none()


def add(db: Session, batch: SubmissionBatch) -> SubmissionBatch:
    """Persist a batch and flush so its id is available to the caller."""
    db.add(batch)
    db.flush()
    return batch


def list_batches_for_project(
    db: Session, project_id: uuid.UUID
) -> list[SubmissionBatch]:
    """Every batch of *project_id*, newest first.

    ``id`` breaks ties: batches opened in the same instant (or the same
    transaction, where ``now()`` does not move) still come back in a stable
    order from one call to the next.
    """
    stmt = (
        select(SubmissionBatch)
        .where(SubmissionBatch.form_project_id == project_id)
        .order_by(SubmissionBatch.created_at.desc(), SubmissionBatch.id.desc())
    )
    return list(db.scalars(stmt).all())


def status_counts_for_project(
    db: Session, project_id: uuid.UUID
) -> dict[uuid.UUID, dict[SubmissionStatus, int]]:
    """{batch_id: {status: submissions in that status}} for every batch of a
    project, in ONE grouped query.

    Batched like :func:`page_counts`: the batch list would otherwise run a
    count per batch. Raw counts only — which statuses mean "reviewed" or
    "blocks export" is a rule, and lives in the service.

    A batch with no submissions is simply ABSENT, as is any status with none; the
    caller reads it with ``.get(batch_id, {})``.
    """
    stmt = (
        select(
            Submission.submission_batch_id,
            Submission.status,
            func.count(Submission.id),
        )
        .join(SubmissionBatch, SubmissionBatch.id == Submission.submission_batch_id)
        .where(SubmissionBatch.form_project_id == project_id)
        .group_by(Submission.submission_batch_id, Submission.status)
    )
    counts: dict[uuid.UUID, dict[SubmissionStatus, int]] = {}
    for batch_id, submission_status, count in db.execute(stmt).all():
        counts.setdefault(batch_id, {})[submission_status] = count
    return counts


def status_counts_for_batch(
    db: Session, batch_id: uuid.UUID
) -> dict[SubmissionStatus, int]:
    """{status: submissions in that status} for one batch, in ONE grouped
    query: the batch-detail and export-gate counterpart of
    :func:`status_counts_for_project`. Raw counts only, a status with none is
    absent."""
    stmt = (
        select(Submission.status, func.count(Submission.id))
        .where(Submission.submission_batch_id == batch_id)
        .group_by(Submission.status)
    )
    return {
        submission_status: count
        for submission_status, count in db.execute(stmt).all()
    }


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------


def add_submission(db: Session, submission: Submission) -> Submission:
    """Persist a submission and flush so its id is available to the caller."""
    db.add(submission)
    db.flush()
    return submission


def get_submission_by_group_key(
    db: Session, batch_id: uuid.UUID, group_key: str
) -> Submission | None:
    """The submission already grouped under *group_key* in this batch, if any.

    The lookup behind cross-call merging: uploading page 1 of a
    submission in one request and page 2 in another must ADD to the same
    Submission, not create a second one. Backed by the partial unique index
    ``uq_submissions_batch_source_group_key``, which also guarantees the "one"
    in one_or_none() at the DB level.

    A NULL *group_key* is never looked up — ungroupable sheets are singletons by
    definition and must never merge with each other — so the caller is expected
    not to call this with one.
    """
    stmt = select(Submission).where(
        Submission.submission_batch_id == batch_id,
        Submission.source_group_key == group_key,
    )
    return db.scalars(stmt).one_or_none()


def taken_page_orders(db: Session, submission_id: uuid.UUID) -> set[int]:
    """``source_page_order`` values already used inside one submission.

    Lets loose-image ingestion append after what earlier upload calls left
    behind, instead of restarting at 0 and colliding with them.
    """
    stmt = select(SubmissionPage.source_page_order).where(
        SubmissionPage.submission_id == submission_id
    )
    return set(db.scalars(stmt).all())


def get_submission(db: Session, submission_id: uuid.UUID) -> Submission | None:
    return db.get(Submission, submission_id)


def get_page(db: Session, page_id: uuid.UUID) -> SubmissionPage | None:
    """One physical sheet by id (for serving its scanned image)."""
    return db.get(SubmissionPage, page_id)


def get_submission_for_update(
    db: Session, submission_id: uuid.UUID
) -> Submission | None:
    """One submission, its row locked ``FOR UPDATE`` until the transaction
    ends.

    Every write of the soft lock or of ``review_version`` reads through here:
    the decision ("is the lock free? is the version current?") and the write
    must see the same row, so two simultaneous opens serialise on it instead of
    both acquiring. ``populate_existing`` because a row already in the identity
    map would otherwise keep its stale attributes — the lock is only as good as
    the values read under it.
    """
    stmt = (
        select(Submission)
        .where(Submission.id == submission_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return db.scalars(stmt).one_or_none()


def list_submissions(
    db: Session,
    batch_id: uuid.UUID,
    status_filter: SubmissionStatus | None = None,
) -> list[Submission]:
    """The batch's submissions, oldest upload first (ingestion order).

    *status_filter* narrows the list to one ``SubmissionStatus`` — the light
    "show me only the MATCH_FAILED ones" view an operator needs to work the
    recovery queue. Omitting it returns the whole batch, unfiltered.
    """
    stmt = select(Submission).where(Submission.submission_batch_id == batch_id)
    if status_filter is not None:
        stmt = stmt.where(Submission.status == status_filter)
    stmt = stmt.order_by(Submission.created_at, Submission.id)
    return list(db.scalars(stmt).all())


def list_unresolved_submissions(
    db: Session, batch_id: uuid.UUID
) -> list[Submission]:
    """The batch's submissions that matching has not fully resolved.

    Unresolved means EITHER no variant yet, OR at least one page still lacking a
    ``form_variant_page_id``. The second half is not redundant: a submission can
    be matched and still gain pages afterwards (a second image upload extending
    it), and a page an earlier run could not place may be placeable once the
    variant is known.

    An UNRESOLVABLE submission is never listed, although it has
    no variant: an operator closed it, and a re-run must not reopen it.

    Row-locked (``FOR UPDATE``) and read fresh (``populate_existing``), so a
    re-run and an operator's mark / undo / manual assignment serialise on each
    row: one marked after this read waits for the run to commit and then finds
    the state the run left, and one marked before is not listed — PostgreSQL
    re-checks the ``status`` filter on the row it waited for.
    The run commits once, so the locks are held until it ends; the other side
    only ever locks a single row, so there is no lock-order deadlock.

    Same ordering as :func:`list_submissions` (ingestion order), so a re-run over
    a batch processes submissions in a stable, human-meaningful sequence.
    """
    pages_pending = select(SubmissionPage.submission_id).where(
        SubmissionPage.form_variant_page_id.is_(None)
    )
    stmt = (
        select(Submission)
        .where(
            Submission.submission_batch_id == batch_id,
            Submission.status != SubmissionStatus.UNRESOLVABLE,
            or_(
                Submission.matched_form_variant_id.is_(None),
                Submission.id.in_(pages_pending),
            ),
        )
        .order_by(Submission.created_at, Submission.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return list(db.scalars(stmt).all())


def list_extractable_submissions(
    db: Session, batch_id: uuid.UUID
) -> list[Submission]:
    """The batch's submissions that answer extraction can actually run on.

    Two conditions, and they are not the same one said twice:

      * ``matched_form_variant_id`` IS NOT NULL — without a variant there is no
        authored page to read the sheet against, so there is nothing to extract;
      * the status is none of ``MATCH_FAILED``, ``UNASSIGNED``, ``REVIEWED``,
        ``FINALIZED``. MATCH_FAILED is implied by the first condition and
        stated anyway, for a reader. UNASSIGNED is NOT implied: matching
        deliberately resolves the variant of an ungroupable sheet WITHOUT
        clearing that status, so such a submission can arrive here
        fully variant-matched, and reading answers off it would attribute them
        to a candidate nobody has yet decided it belongs to. REVIEWED and
        FINALIZED are closed by a human: a re-run must never add readings to
        them — reopening brings one back to SCANNED first.

    Same ordering as :func:`list_submissions` (ingestion order), so a run over a
    batch processes submissions in a stable, human-meaningful sequence.

    ``pages`` is eager-loaded: the orchestrator walks every page of every
    submission, and that relationship is the one genuine N+1 on this path. The
    AUTHORED side it reaches through each page (the template page and its
    objects) is not eager-loaded on purpose — every submission of a batch shares
    the same handful of template pages, so the session's identity map already
    loads each of them exactly once, however many sheets point at it.
    """
    stmt = (
        select(Submission)
        .where(
            Submission.submission_batch_id == batch_id,
            Submission.matched_form_variant_id.is_not(None),
            Submission.status.not_in(
                (
                    SubmissionStatus.MATCH_FAILED,
                    SubmissionStatus.UNASSIGNED,
                    SubmissionStatus.REVIEWED,
                    SubmissionStatus.FINALIZED,
                )
            ),
        )
        .options(selectinload(Submission.pages))
        .order_by(Submission.created_at, Submission.id)
    )
    return list(db.scalars(stmt).all())


def list_open_submissions_without_candidate(
    db: Session, batch_id: uuid.UUID
) -> list[Submission]:
    """The batch's submissions a respondent-ID reading may still give a
    candidate to: open (MATCHED or SCANNED), with no candidate, and on which no
    review save has happened yet (``review_version`` 0: once a human has
    worked on it, an empty candidate is their state). The rule itself is
    ``submission_candidate_service``'s; this only narrows the walk of a
    threshold change to the submissions it can apply to. Ingestion order, like
    :func:`list_submissions`.

    Row-locked (``FOR UPDATE``) and read fresh (``populate_existing``): the
    candidate is set on these rows, and a review save committed meanwhile must
    not be overwritten from a stale copy. Under READ
    COMMITTED Postgres re-checks the conditions on a row it had to wait for.
    """
    stmt = (
        select(Submission)
        .where(
            Submission.submission_batch_id == batch_id,
            Submission.status.in_(
                (SubmissionStatus.MATCHED, SubmissionStatus.SCANNED)
            ),
            or_(
                Submission.respondent_identifier.is_(None),
                Submission.respondent_identifier == "",
            ),
            Submission.review_version == 0,
        )
        .order_by(Submission.created_at, Submission.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return list(db.scalars(stmt).all())


def page_counts(db: Session, batch_id: uuid.UUID) -> dict[uuid.UUID, int]:
    """{submission_id: page count} for one batch, in ONE grouped query.

    Batched deliberately: the detail endpoint would otherwise trigger an N+1
    over ``Submission.pages`` just to report a number.
    """
    stmt = (
        select(SubmissionPage.submission_id, func.count(SubmissionPage.id))
        .join(Submission, Submission.id == SubmissionPage.submission_id)
        .where(Submission.submission_batch_id == batch_id)
        .group_by(SubmissionPage.submission_id)
    )
    return {submission_id: count for submission_id, count in db.execute(stmt).all()}


def present_page_indices_for_batch(
    db: Session, batch_id: uuid.UUID
) -> dict[uuid.UUID, set[int]]:
    """{submission_id: template page_index values its sheets are bound to} for
    one batch, in ONE query.

    The "present" half of the missing-pages computation, batched like
    :func:`page_counts` so the batch detail does not lazy-load
    ``Submission.pages`` and each ``form_variant_page`` per submission, which is
    what the ``Submission.missing_page_indices`` property would do.

    Bound sheets only (an INNER join on ``form_variant_page_id``): a sheet
    matching could not place — including an extra sheet beyond the variant's
    last page, left unbound at confidence 0.0 — fills no template slot. A
    submission with no bound sheet is simply ABSENT; the caller reads it with
    ``.get(submission_id, set())``.
    """
    stmt = (
        select(SubmissionPage.submission_id, FormVariantPage.page_index)
        .join(FormVariantPage, FormVariantPage.id == SubmissionPage.form_variant_page_id)
        .join(Submission, Submission.id == SubmissionPage.submission_id)
        .where(Submission.submission_batch_id == batch_id)
        .distinct()
    )
    present: dict[uuid.UUID, set[int]] = {}
    for submission_id, page_index in db.execute(stmt).all():
        present.setdefault(submission_id, set()).add(page_index)
    return present


def list_pages_with_template(
    db: Session, submission_id: uuid.UUID
) -> list[SubmissionPage]:
    """Every sheet of one submission, its template page loaded, in ONE query.

    For the submission review page, which lists EVERY sheet, placed or not: the
    template page comes through an OUTER join, so a sheet matching could not
    bind is still returned, with ``form_variant_page`` None.

    Ordered by ``(source_page_order, id)``: ``source_page_order`` is NOT unique
    within a submission (see ``SubmissionPage``), and ``id`` makes two sheets
    that declared the same position come back in the same order on every read.
    """
    stmt = (
        select(SubmissionPage)
        .where(SubmissionPage.submission_id == submission_id)
        .options(joinedload(SubmissionPage.form_variant_page))
        .order_by(SubmissionPage.source_page_order, SubmissionPage.id)
    )
    return list(db.scalars(stmt).all())


def list_bound_pages_for_submissions(
    db: Session, submission_ids: Collection[uuid.UUID]
) -> list[SubmissionPage]:
    """The sheets of *submission_ids* that matching bound to a template page,
    in ONE query.

    Bound sheets only: an unplaced sheet has no template objects, so nothing on
    it can be read — or be unread. Feeds the "unread objects" computation for
    the queue (many submissions) and for "Review anyway" (one). Empty input
    returns [] without touching the database.
    """
    if not submission_ids:
        return []
    stmt = (
        select(SubmissionPage)
        .where(
            SubmissionPage.submission_id.in_(submission_ids),
            SubmissionPage.form_variant_page_id.is_not(None),
        )
        .order_by(SubmissionPage.source_page_order, SubmissionPage.id)
    )
    return list(db.scalars(stmt).all())
