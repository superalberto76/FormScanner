# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import MatchFailureReason, SubmissionStatus
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.form_variant import FormVariant
    from app.models.submission_batch import SubmissionBatch
    from app.models.submission_page import SubmissionPage


class Submission(Base, TimestampMixin):
    """Level 4 — a candidate's whole submission within a SubmissionBatch.

    One Submission is one candidate against one FormVariant, made of N physical
    ``SubmissionPage`` rows. Per-physical-page fields (source file, identified
    code) live on SubmissionPage, not here.
    """

    __tablename__ = "submissions"

    # Two submissions in one batch can never share a grouping key — that is the
    # whole point of the key: it is what makes a second upload call ADD to an
    # existing submission instead of creating a duplicate. The uniqueness is
    # PARTIAL because an UNGROUPABLE sheet has key NULL, and a batch
    # may hold any number of those; a plain UNIQUE would let exactly one through
    # on some engines and none of them collide meaningfully on Postgres, so the
    # WHERE clause states the intent explicitly.
    __table_args__ = (
        Index(
            "uq_submissions_batch_source_group_key",
            "submission_batch_id",
            "source_group_key",
            unique=True,
            postgresql_where=text("source_group_key IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    submission_batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("submission_batches.id"),
        nullable=False,
        index=True,
    )
    # Resolved once the sheet is matched to a printable variant; null until then.
    matched_form_variant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("form_variants.id"),
        nullable=True,
        index=True,
    )
    status: Mapped[SubmissionStatus] = mapped_column(
        SAEnum(SubmissionStatus, name="submission_status_enum"),
        nullable=False,
        default=SubmissionStatus.UPLOADED,
        index=True,
    )
    # Human/respondent identifier resolved from the code or review.
    respondent_identifier: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )
    # TECHNICAL grouping key for loose-image ingestion: what made these sheets
    # one submission. Either a decoded RID value or the <submission> token of a
    # conforming filename — whichever resolved the grouping, with no
    # record here of which (that is audit's job).
    #
    # Deliberately SEPARATE from respondent_identifier, which stays the human /
    # domain fact ("who filled this in") and is populated from a RID
    # independently of the grouping key. They coincide when a RID did the grouping and diverge
    # when a filename did (key set, respondent still unknown) — collapsing them
    # would make a filename token masquerade as an identified candidate.
    #
    # NULL for PDF ingestion (one upload = one submission, nothing to group by)
    # and for an UNGROUPABLE image, whose Submission is UNASSIGNED. See the
    # partial unique index in __table_args__.
    source_group_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    total_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Why matching could not resolve the variant: written by
    # ``submission_matching_service`` when Level 1 fails, cleared when the
    # variant resolves (automatically or by manual assignment). Kept through
    # UNRESOLVABLE, so an undone verdict shows it again. NULL also on a
    # MATCH_FAILED submission matched before this column existed, until the
    # next matching run writes it.
    match_failure_reason: Mapped[MatchFailureReason | None] = mapped_column(
        SAEnum(MatchFailureReason, name="match_failure_reason_enum"),
        nullable=True,
    )
    # With VID_NOT_RECOGNISED only: the first identifier value read, in
    # physical page order, that belongs to no published variant.
    match_failure_code_value: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )
    # The status an UNRESOLVABLE submission was marked from: MATCH_FAILED or
    # UNASSIGNED, the status "Undo" brings it back to. Written by
    # ``submission_batch_service.mark_unresolvable``, cleared by the undo;
    # NULL on every submission that is not UNRESOLVABLE. It cannot be derived:
    # an UNASSIGNED submission may carry a variant and a failure reason too,
    # since matching spares only its status.
    unresolvable_from_status: Mapped[SubmissionStatus | None] = mapped_column(
        SAEnum(SubmissionStatus, name="submission_status_enum"),
        nullable=True,
    )

    # Review concurrency — two complementary mechanisms, both per submission;
    # the rules live in ``submission_lock_service``.
    #
    # SOFT LOCK: who is working on this submission right now. ``locked_at`` is the
    # time of the holder's LAST ACTIVITY (open, renewal, review save), not of
    # the first open: the lock is active while ``locked_by`` is set and less than
    # ``LOCK_TIMEOUT`` has passed since ``locked_at``. An expired lock is
    # IGNORED, never cleared — no job sweeps these columns — so a stale holder
    # and timestamp can sit in the row until the next acquire overwrites them.
    # Read them through the service, never as "locked_by IS NOT NULL".
    locked_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # OPTIMISTIC CONCURRENCY: incremented by every review save. A save declares
    # the version it was built on and is refused if the submission has moved
    # since, so nobody ever saves from a stale screen — even one that still
    # holds (or re-acquired) the lock. 0 for a submission never reviewed.
    review_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # The last time an extraction run EXAMINED this submission, whatever came
    # of it — errors included. Written by ``extraction_orchestrator_service``
    # on every submission its walk reaches.
    # NULL means no run has ever examined it, which is the one fact a MATCHED
    # status cannot tell apart from "extracted, with errors" — and "Review
    # anyway" is allowed only on the latter. No backfill: a submission extracted
    # before this column existed gets a value on its next run.
    extraction_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    submission_batch: Mapped[SubmissionBatch] = relationship(
        "SubmissionBatch", back_populates="submissions"
    )
    matched_form_variant: Mapped[FormVariant | None] = relationship(
        "FormVariant", back_populates="submissions"
    )
    # Ordered by the physical position in the uploaded PDF, so a submission always
    # serialises its pages in a deterministic, human-meaningful order.
    pages: Mapped[list[SubmissionPage]] = relationship(
        "SubmissionPage",
        back_populates="submission",
        cascade="all, delete-orphan",
        order_by="SubmissionPage.source_page_order",
    )

    @property
    def missing_page_indices(self) -> list[int]:
        """Template page indices expected but not yet present as SubmissionPage.

        Empty list (not None) if the submission has not been matched to a variant
        yet — there is no template to compare against, so it is not "incomplete"
        for lack of data. Requires ``matched_form_variant.pages`` and
        ``self.pages`` (with ``form_variant_page``) loaded/loadable; access may
        trigger lazy-load.
        """
        if self.matched_form_variant is None:
            return []
        expected = {p.page_index for p in self.matched_form_variant.pages}
        present = {
            sp.form_variant_page.page_index
            for sp in self.pages
            if sp.form_variant_page is not None
        }
        return sorted(expected - present)

    def __repr__(self) -> str:
        return f"<Submission id={self.id} status={self.status}>"
