# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Enum as SAEnum, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import ReviewAction
from app.models.mixins import CreatedAtMixin

if TYPE_CHECKING:
    from app.models.submission_object_result import SubmissionObjectResult
    from app.models.user import User


class SubmissionReview(Base, CreatedAtMixin):
    """Level 4 — an append-only log entry recording one human review action.

    This is the HISTORY of a result's review, not its state:
    every action — CONFIRM, SUBSTITUTE, FLAG and UNDO alike — appends one row,
    and nothing ever updates or deletes one. The CURRENT ruling lives on the
    result itself (``SubmissionObjectResult.review_outcome`` and friends), and
    "resolved" is read from there, never from the existence of a row here: an
    UNDONE result has rows and is unresolved. The log IS the trail of review
    actions, which is why they write no ``audit_logs`` event of their own.
    """

    __tablename__ = "submission_reviews"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    submission_object_result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("submission_object_results.id"),
        nullable=False,
        index=True,
    )
    reviewer_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    # What the operator did. Rows written before this column existed were set
    # to CONFIRMED when it was added.
    action: Mapped[ReviewAction] = mapped_column(
        SAEnum(ReviewAction, name="review_action_enum"), nullable=False
    )
    # The note given WITH this action, trimmed; null when none.
    review_note: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    # The value a SUBSTITUTED action entered, exactly as it was written onto
    # the result's ``reviewed_payload_json``; null for every other action.
    reviewed_payload_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Relationships
    object_result: Mapped[SubmissionObjectResult] = relationship(
        "SubmissionObjectResult", back_populates="reviews"
    )
    reviewer_user: Mapped[User] = relationship(
        "User", foreign_keys=[reviewer_user_id]
    )

    def __repr__(self) -> str:
        return f"<SubmissionReview id={self.id}>"
