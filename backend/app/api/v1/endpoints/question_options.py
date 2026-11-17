# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Question option item endpoints.

Mounted at /question-options. Collection routes (list/create under an object)
live in page_objects.py.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.deps import get_db, require_user
from app.models.user import User
from app.schemas.question_object import QuestionOptionRead, QuestionOptionUpdate
from app.services import question_object_service

router = APIRouter()


@router.patch("/{option_id}", response_model=QuestionOptionRead)
def update_question_option(
    option_id: uuid.UUID,
    data: QuestionOptionUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Update an option (DRAFT only). 409 on duplicate order_index."""
    return question_object_service.update_option(db, option_id, data, user)


@router.delete("/{option_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_question_option(
    option_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Delete an option (DRAFT only)."""
    question_object_service.delete_option(db, option_id, user)
