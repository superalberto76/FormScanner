# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only audit log endpoint.

ADMIN → can query all rows, optionally filtered.
USER  → can only query rows where actor_user_id == their own id.
        TODO: extend to include rows for entities they can VIEW (owner/share).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import get_db, require_user
from app.models.enums import UserRole
from app.models.user import User
from app.repositories import audit_repo
from app.schemas.audit_log import AuditLogRead

router = APIRouter()


@router.get("", response_model=list[AuditLogRead])
def list_audit_logs(
    entity_type: Annotated[str | None, Query()] = None,
    entity_id: Annotated[uuid.UUID | None, Query()] = None,
    action: Annotated[str | None, Query()] = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Query audit log entries.

    - **ADMIN**: full access, all rows, all filters.
    - **USER**: restricted to rows where `actor_user_id` equals the caller's id.
    """
    # Non-admin users are always scoped to their own actor_user_id.
    actor_filter: uuid.UUID | None
    if user.role is UserRole.ADMIN:
        actor_filter = None  # no restriction
    else:
        actor_filter = user.id

    return audit_repo.query(
        db,
        actor_user_id=actor_filter,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        action=action,
        from_dt=from_,
        to_dt=to,
        limit=limit,
        offset=offset,
    )
