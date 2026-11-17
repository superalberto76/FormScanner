# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import FormProjectStatus, FormProjectType
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.form_project_member import FormProjectMember
    from app.models.form_variant import FormVariant
    from app.models.submission_batch import SubmissionBatch
    from app.models.user import User


class FormProject(Base, TimestampMixin):
    """Level 1 — a general project/campaign (exam, survey, questionnaire, form).

    Not limited to OMR exams. It owns one or more ``FormVariant`` rows (the
    printable forms).
    """

    __tablename__ = "form_projects"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # PRIMARY/CREATOR marker (first avatar in the member stack), NOT the access
    # source — access is decided by FormProjectMember rows.
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    type: Mapped[FormProjectType] = mapped_column(
        SAEnum(FormProjectType, name="form_project_type_enum"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    # Optional date the campaign refers to (exam date, survey window, …).
    event_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[FormProjectStatus] = mapped_column(
        SAEnum(FormProjectStatus, name="form_project_status_enum"),
        nullable=False,
        default=FormProjectStatus.DRAFT,
        index=True,
    )

    # Relationships
    # NOTE: no back_populates on User — the User model intentionally carries
    # no relationship back to its projects.
    owner: Mapped[User] = relationship("User", foreign_keys=[owner_id])
    # Role-based membership is the access source of truth. Deleting a project
    # removes its memberships (alongside variants/submission_batches).
    members: Mapped[list[FormProjectMember]] = relationship(
        "FormProjectMember",
        back_populates="project",
        cascade="all, delete-orphan",
    )
    variants: Mapped[list[FormVariant]] = relationship(
        "FormVariant",
        back_populates="form_project",
        cascade="all, delete-orphan",
        order_by="FormVariant.version_number",
    )
    submission_batches: Mapped[list[SubmissionBatch]] = relationship(
        "SubmissionBatch",
        back_populates="form_project",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<FormProject id={self.id} type={self.type} name={self.name!r}>"
