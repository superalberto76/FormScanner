# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.mixins import CreatedAtMixin

if TYPE_CHECKING:
    from app.models.user import User


class AuditLog(Base, CreatedAtMixin):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Null when the action is performed by the system (e.g. background job).
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True
    )
    # Verb describing the event, e.g. "template.published", "area.configured".
    action: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # Class name of the affected entity, e.g. "TemplateVersion", "SensitiveArea".
    entity_type: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # UUID of the affected entity as a string; null for non-entity events.
    entity_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    # Arbitrary structured payload for the event (before/after state, diffs, etc.).
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Relationships
    actor: Mapped[User | None] = relationship("User", foreign_keys=[actor_user_id])
