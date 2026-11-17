# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The maximum score of a FormVariant, for authoring.

Loads the variant's scorable objects in a constant number of queries and hands
them to ``scoring_rules.variant_max_score``, the one place the formula and the
choice of objects live. Read-only, nothing persisted: the value is computed on
the fly in any status of the variant.
"""
from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.user import User
from app.repositories import form_variant_repo, page_object_repo
from app.schemas.form_variant import VariantMaxScoreRead
from app.services import scoring_rules
from app.services.form_project_service import require_project_member


def get_max_score(
    db: Session, variant_id: uuid.UUID, user: User
) -> VariantMaxScoreRead:
    """The maximum score of *variant_id*; 404 / 403 as reading the variant."""
    variant = form_variant_repo.get(db, variant_id)
    if variant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Form variant not found"
        )
    require_project_member(db, variant.form_project_id, user)

    page_objects = page_object_repo.list_scorable_for_variant(db, variant_id)
    return VariantMaxScoreRead(max_score=scoring_rules.variant_max_score(page_objects))
