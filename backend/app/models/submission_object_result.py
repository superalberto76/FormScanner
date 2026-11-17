# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import ReviewOutcome, SubmissionResultType
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.page_object import PageObject
    from app.models.submission_page import SubmissionPage
    from app.models.submission_review import SubmissionReview


class SubmissionObjectResult(Base, TimestampMixin):
    """Level 4 — per-object detection/scoring result for one submission page.

    Each PageObject lives on a specific physical page, so a result hangs off a
    ``SubmissionPage`` rather than the whole Submission. Links back to
    the authored PageObject it was detected for.
    Detected vs reviewed payloads are kept as JSON because their shape varies
    by ``result_type`` (selected options, matrix cells, OCR text, code value).
    """

    __tablename__ = "submission_object_results"
    __table_args__ = (
        # One authored object, read off one physical sheet, yields exactly ONE
        # result. Extraction is re-runnable, and without this the
        # second run would silently append a duplicate reading of the same
        # bubble instead of being refused — leaving two rows for one fact, with
        # nothing in the data to say which of them the review queue should
        # believe. Enforced in the DB rather than by a service check, because
        # the guarantee has to hold for every future writer of this table.
        UniqueConstraint(
            "submission_page_id",
            "page_object_id",
            name="uq_submission_object_result_page_object",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    submission_page_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("submission_pages.id"),
        nullable=False,
        index=True,
    )
    page_object_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("page_objects.id"),
        nullable=False,
        index=True,
    )
    result_type: Mapped[SubmissionResultType] = mapped_column(
        SAEnum(SubmissionResultType, name="submission_result_type_enum"),
        nullable=False,
        index=True,
    )
    # Raw machine detection (pre-review). Shape depends on result_type.
    #
    # NULL marks a PLACEHOLDER ("Review anyway"): a result created
    # for an object no run could read, so a human enters its answer from the
    # paper sheet. That is unambiguous because every extractor ALWAYS writes a
    # payload — even an empty reading (nothing marked, nothing legible, a
    # degenerate grid) is a payload saying so — so NULL can only mean "no
    # reading exists". A placeholder counts as extracted: a later run does not
    # retry it.
    detected_payload_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Human-corrected payload: the value a SUBSTITUTE entered, in
    # the per-type shape of ``app.schemas.submission_result`` (the reviewed
    # payloads), validity always computed by the server. NULL for every other
    # outcome and for an unresolved result.
    reviewed_payload_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    auto_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    final_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # --- The two axes ``review_required`` derives from ----------------------
    # Both are FACTS OF THE OPTICAL READING, written once by the extractor and
    # never rewritten afterwards — in particular the threshold recomputation
    # reads them and touches neither.
    #
    # The aggregated confidence of this reading, in [0, 1]. For a QUESTION (and
    # for one row of a MATRIX) it is the MINIMUM confidence among the options
    # read as selected: a question is only as sure as its least sure bubble.
    # NULL means extraction has never produced a confidence here — either the
    # result predates extraction, or its type has no such axis (a CODE reading
    # is not scored this way). NULL is deliberately NOT 0.0: an unknown
    # confidence must never, on its own, send a result to review.
    confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # The other axis, independent of confidence: "a human is needed whatever
    # the confidence". True when the reading's ``Validity`` is not VALID (two
    # bubbles on a single-choice question, and so on), when the object type
    # ALWAYS needs a human (FREE_TEXT_AREA), when a CODE did not
    # decode, or when a RESPONDENT_IDENTIFIER grid has an ambiguous or empty
    # column or could not be read at all — the candidate attribution is the most
    # sensitive fact of a submission, so a digit the system had to choose is
    # never accepted unseen. Forcing review HERE rather than on
    # ``review_required`` is what makes it survive threshold changes, which
    # re-derive that flag from the two axes only. NOT NULL: unlike a
    # confidence, "unknown" has no meaning here — an object either forces
    # review or it does not.
    validity_forces_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # A third fact, of the PAGE rather than of the reading:
    # the sheet this was read on could not be aligned to its template, so the
    # reading was taken in proportion and may be shifted. Copied from
    # ``SubmissionPage.alignment_status`` by the extractor, like the two axes
    # above it is never rewritten, and it forces review whatever the
    # confidence and the threshold (``confidence_threshold_service``).
    sheet_not_aligned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # Derived from the facts above — (confidence below the effective
    # threshold) OR validity_forces_review OR sheet_not_aligned — but
    # PERSISTED, not computed on read. It has to be: once a human has reviewed this result, the value it
    # carried at review time is frozen forever, and a computed column would
    # silently rewrite that history every time the batch's threshold moved.
    # Written by the extractor at creation and, for not-yet-reviewed results
    # only, by ``confidence_threshold_service`` when the threshold changes.
    review_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # SUPERSEDED by ``review_outcome`` below and unused: nothing writes it and
    # nothing reads it. NOT the source of truth for "has a human ruled on
    # this". Kept only because dropping it is a separate change.
    reviewed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # --- The CURRENT human ruling ---------------------------------------------
    # The current state of review lives here, on the result, so the review page
    # reads it directly; the HISTORY of every action (UNDO included) is the
    # append-only ``submission_reviews`` log. The two never disagree because
    # every action writes both in one transaction
    # (``review_action_service``).
    #
    # "Resolved" means ``review_outcome IS NOT NULL`` — everywhere: the
    # threshold recompute, the pending counts, the review read. NULL is "not
    # resolved": never ruled on, or ruled on and then UNDONE, which makes the
    # result fair game for the threshold recompute again.
    review_outcome: Mapped[ReviewOutcome | None] = mapped_column(
        SAEnum(ReviewOutcome, name="review_outcome_enum"), nullable=True
    )
    # The operator's note on the CURRENT ruling, shown to colleagues with its
    # author and time. Earlier notes stay in the log only.
    review_note: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    # Who made the current ruling, and when (UTC). All three, with
    # ``review_outcome`` and ``reviewed_payload_json``, return to NULL on UNDO.
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    submission_page: Mapped[SubmissionPage] = relationship(
        "SubmissionPage", back_populates="object_results"
    )
    page_object: Mapped[PageObject] = relationship(
        "PageObject", back_populates="submission_results"
    )
    reviews: Mapped[list[SubmissionReview]] = relationship(
        "SubmissionReview",
        back_populates="object_result",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<SubmissionObjectResult id={self.id} type={self.result_type}>"
