# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Enum as SAEnum, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import UITheme, UserRole
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.group import GroupMembership


class User(Base, TimestampMixin):
    __tablename__ = "users"
    # Uniqueness of the email is a named constraint, as the migration creates
    # it: no separate index.
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # Single-name column, kept nullable for backward compatibility. Users are
    # created with first_name/last_name instead; full_name is not populated or
    # derived here.
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # True until the user changes the admin-assigned initial password.
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    role: Mapped[UserRole] = mapped_column(
        SAEnum(UserRole, name="user_role_enum"),
        nullable=False,
        default=UserRole.USER,
    )
    disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Per-user UI theme preference. Stored as the MODE, not the
    # resolved colour: SYSTEM follows the OS at runtime. NOT NULL, defaults to
    # SYSTEM so the choice follows the user across devices.
    ui_theme: Mapped[UITheme] = mapped_column(
        SAEnum(UITheme, name="ui_theme_enum"),
        nullable=False,
        default=UITheme.SYSTEM,
    )

    # Profile avatar. Mirrors ImageObjectConfig's file fields
    # but on the user. ``avatar_path`` is an absolute on-disk path and is NEVER
    # exposed by the API — schemas expose ``has_avatar`` instead. All three are
    # NULL until the user uploads a photo (initials fallback in the UI).
    avatar_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    avatar_mime: Mapped[str | None] = mapped_column(String(100), nullable=True)
    avatar_original_filename: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )

    @property
    def has_avatar(self) -> bool:
        return self.avatar_path is not None

    # Relationships
    memberships: Mapped[list[GroupMembership]] = relationship(
        "GroupMembership",
        back_populates="user",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email!r}>"
