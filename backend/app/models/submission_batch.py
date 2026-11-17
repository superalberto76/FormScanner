# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Float, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.mixins import CreatedAtMixin

if TYPE_CHECKING:
    from app.models.form_project import FormProject
    from app.models.submission import Submission
    from app.models.user import User


class SubmissionBatch(Base, CreatedAtMixin):
    """Level 4 — a batch of scanned sheets uploaded against a FormProject."""

    __tablename__ = "submission_batches"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    form_project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("form_projects.id"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    # This batch's override of ``settings.default_confidence_threshold``:
    # a result read with less confidence than this needs a human.
    # NULL is a MEANINGFUL value, not a missing one — "follow the system
    # default", so a later change to that default reaches this batch. Resolve it
    # through ``confidence_threshold_service.effective_threshold`` rather than
    # reading it raw. The [0, 1] range is validated at the API boundary, not by
    # the DB, as everywhere else in this domain.
    confidence_threshold: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )

    # Relationships
    form_project: Mapped[FormProject] = relationship(
        "FormProject", back_populates="submission_batches"
    )
    submissions: Mapped[list[Submission]] = relationship(
        "Submission",
        back_populates="submission_batch",
        cascade="all, delete-orphan",
    )
    created_by_user: Mapped[User] = relationship(
        "User", foreign_keys=[created_by_user_id]
    )

    def __repr__(self) -> str:
        return f"<SubmissionBatch id={self.id} name={self.name!r}>"
