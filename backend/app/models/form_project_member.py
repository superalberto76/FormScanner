# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Enum as SAEnum, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import ProjectRole
from app.models.mixins import CreatedAtMixin

if TYPE_CHECKING:
    from app.models.form_project import FormProject
    from app.models.user import User


class FormProjectMember(Base, CreatedAtMixin):
    """Membership row linking a User to a FormProject with a role.

    SOURCE OF TRUTH for FormProject access: a user may act on a project iff a
    row exists here (admins bypass). Mirrors ``GroupMembership`` — append-only
    ``CreatedAtMixin``, a unique (project, user) pair, and a uni-directional
    ``user`` relationship that adds nothing to the ``User`` model (just as
    ``FormProject.owner`` does).
    """

    __tablename__ = "form_project_members"
    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_form_project_member"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("form_projects.id"),
        nullable=False,
        # No separate index: the unique constraint (project_id, user_id) covers
        # prefix lookups by project_id.
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    role: Mapped[ProjectRole] = mapped_column(
        SAEnum(ProjectRole, name="project_role_enum"),
        nullable=False,
        default=ProjectRole.COLLABORATOR,
    )

    # Relationships
    project: Mapped[FormProject] = relationship(
        "FormProject", back_populates="members"
    )
    # Uni-directional (no back_populates) — the User model carries no
    # relationship back, mirroring how FormProject.owner references User.
    user: Mapped[User] = relationship("User", foreign_keys=[user_id])

    def __repr__(self) -> str:
        return (
            f"<FormProjectMember project_id={self.project_id} "
            f"user_id={self.user_id} role={self.role}>"
        )
