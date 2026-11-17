# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Enum as SAEnum, Float, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import PageAlignmentStatus, PageOrderSource
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.form_variant_page import FormVariantPage
    from app.models.submission import Submission
    from app.models.submission_object_result import SubmissionObjectResult


class SubmissionPage(Base, TimestampMixin):
    """Level 4 — a single physical scanned page within a Submission.

    A Submission is the candidate's whole set of sheets (1 candidate, 1
    FormVariant, N physical pages); each SubmissionPage is one of those physical
    pages. It is linked to the template ``FormVariantPage`` only once matching
    resolves it (nullable until then).
    """

    __tablename__ = "submission_pages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    submission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("submissions.id"),
        nullable=False,
        index=True,
    )
    # Nullable: resolved only after matching. No ON DELETE
    # CASCADE needed in this direction — it is the physical page that
    # (optionally) references the template, not the other way around.
    form_variant_page_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("form_variant_pages.id"),
        nullable=True,
        index=True,
    )
    source_file_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    source_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    # Code (QR/barcode) value read off this physical page, used for matching.
    # The RAW payload as decoded (``VID:<value>``, or ``VID:<value>|<page_index>``
    # when the code carries a page index), not the parsed value — matching
    # re-parses it with ``app.utils.code_rendering.parse_tagged_payload``.
    # Only a code tagged as the VARIANT_IDENTIFIER lands here: a
    # respondent identifier goes to ``Submission.respondent_identifier``, and an
    # untagged code identifies nothing, so this stays NULL.
    identified_code_value: Mapped[str | None] = mapped_column(
        String(1000), nullable=True
    )
    # 0-based position of this page inside its submission. ALWAYS populated,
    # whatever the outcome of code decoding — it is the submission's fallback
    # ordering and the stable identity of "the Nth sheet we received". For a PDF
    # it is the physical page order; for loose images it is whatever
    # the code, the filename or the arrival order declared.
    #
    # NOT UNIQUE within a Submission, by design — there is deliberately no
    # constraint enforcing it, and matching MUST NOT assume there is.
    # Loose-image ingestion honours a DECLARED position verbatim, so two sheets
    # can legitimately land on the same value: two files named
    # ``smith_page_1.jpg``, or two masters printed with the same VID page
    # index. Ingestion keeps both pages rather than moving, overwriting or
    # rejecting one (see submission_batch_service._GroupResolver.take_page_order)
    # and reports the clash in the audit trail under
    # ``pages_with_duplicate_position``. A consumer that needs a total order must
    # break ties itself — ``created_at``/``id`` are stable tie-breakers.
    source_page_order: Mapped[int] = mapped_column(Integer, nullable=False)
    # Provenance of the ordering: CODE when a page index was read off the code,
    # PDF_ORDER when the physical order had to be used instead.
    page_order_source: Mapped[PageOrderSource] = mapped_column(
        SAEnum(PageOrderSource, name="page_order_source_enum"),
        nullable=False,
        default=PageOrderSource.PDF_ORDER,
    )
    # How much the ``form_variant_page_id`` above is worth. Written
    # ONLY by the matching engine, so NULL means "matching has never run on this
    # page", which is a different fact from "matching ran and found nothing":
    #
    #   * 1.0  — the page's own VID code carried a page index and a
    #            FormVariantPage with that index exists in the matched variant.
    #            The sheet DECLARED its position; nothing was inferred.
    #   * 0.5  — no usable index in the code, so the position was deduced from
    #            ``source_page_order + 1`` (physical order, 0-based -> the
    #            1-based FormVariantPage.page_index). A plausible guess, not a
    #            statement by the sheet: a missing or reordered physical page
    #            silently shifts it.
    #   * 0.0  — the wanted page_index does not exist in the matched variant
    #            (more physical sheets than the variant has pages, or a code
    #            claiming an out-of-range index). ``form_variant_page_id`` stays
    #            NULL; the page is unplaced but never blocks its siblings.
    #
    # A float rather than an enum on purpose: the ladder above is expected to
    # gain intermediate evidence (per-page RID, layout fingerprint) without a
    # migration, and consumers should threshold it rather than switch on it.
    match_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    # --- Alignment to the template ------------------------------------------
    # Written by the extraction run (``page_alignment_service``) once per
    # page, before its objects are read, and reused by every later run: all
    # the readings of a page stand on the same image. NULL = never attempted
    # (not extracted yet, or read before alignment existed, which nothing
    # backfills).
    alignment_status: Mapped[PageAlignmentStatus | None] = mapped_column(
        SAEnum(PageAlignmentStatus, name="page_alignment_status_enum"),
        nullable=True,
    )
    # The scan straightened into the template's frame, at the scan's
    # resolution (PNG, ``{id}_aligned.png`` in SUBMISSION_PAGES_DIR). Only
    # with ALIGNED; the extractors read it instead of ``source_file_path``.
    # The original is never touched.
    aligned_file_path: Mapped[str | None] = mapped_column(
        String(1000), nullable=True
    )
    # Diagnostics only, never read by a rule: the marks found per scan
    # corner, upside down or not, the transformation, rotation, scales,
    # shear, residual and, for NOT_ALIGNED, the reason
    # (``sheet_alignment.Alignment.to_json``).
    alignment_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Relationships
    submission: Mapped[Submission] = relationship(
        "Submission", back_populates="pages"
    )
    # Unidirectional: no back_populates on FormVariantPage — that model is out
    # of scope and must not gain a reverse relationship here.
    form_variant_page: Mapped[FormVariantPage | None] = relationship(
        "FormVariantPage"
    )
    object_results: Mapped[list[SubmissionObjectResult]] = relationship(
        "SubmissionObjectResult",
        back_populates="submission_page",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<SubmissionPage id={self.id} submission_id={self.submission_id}>"
