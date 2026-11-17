# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Data-access layer for FormProject."""
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.enums import FormVariantStatus
from app.models.form_project import FormProject
from app.models.form_project_member import FormProjectMember
from app.models.form_variant import FormVariant


def get(db: Session, project_id: uuid.UUID) -> FormProject | None:
    return db.get(FormProject, project_id)


def add(db: Session, project: FormProject) -> FormProject:
    db.add(project)
    db.flush()  # populate project.id
    return project


def list_owned(db: Session, owner_id: uuid.UUID) -> list[FormProject]:
    """All projects owned by *owner_id*, newest first."""
    stmt = (
        select(FormProject)
        .where(FormProject.owner_id == owner_id)
        .order_by(FormProject.created_at.desc())
    )
    return list(db.scalars(stmt).all())


def list_for_member(db: Session, user_id: uuid.UUID) -> list[FormProject]:
    """All projects where *user_id* is a member (any role), newest first.

    Membership is the access source of truth: this powers the
    non-admin project list so collaborators see shared projects too.
    """
    stmt = (
        select(FormProject)
        .join(FormProjectMember, FormProjectMember.project_id == FormProject.id)
        .where(FormProjectMember.user_id == user_id)
        .order_by(FormProject.created_at.desc())
    )
    return list(db.scalars(stmt).all())


def list_all(db: Session) -> list[FormProject]:
    """All projects (admin view), newest first."""
    stmt = select(FormProject).order_by(FormProject.created_at.desc())
    return list(db.scalars(stmt).all())


def variant_counts(
    db: Session, project_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Map of project_id -> number of variants, for the given project ids."""
    if not project_ids:
        return {}
    stmt = (
        select(FormVariant.form_project_id, func.count(FormVariant.id))
        .where(FormVariant.form_project_id.in_(project_ids))
        .group_by(FormVariant.form_project_id)
    )
    return {pid: count for pid, count in db.execute(stmt).all()}


def count_variants(db: Session, project_id: uuid.UUID) -> int:
    count = db.scalar(
        select(func.count(FormVariant.id)).where(
            FormVariant.form_project_id == project_id
        )
    )
    return count or 0


def count_non_draft_variants(db: Session, project_id: uuid.UUID) -> int:
    """Number of variants of *project_id* whose status is NOT DRAFT.

    Used by the delete gate: a project may only be deleted when every variant
    is still in DRAFT (so deleting it never cascades away published content).
    """
    count = db.scalar(
        select(func.count(FormVariant.id)).where(
            FormVariant.form_project_id == project_id,
            FormVariant.status != FormVariantStatus.DRAFT,
        )
    )
    return count or 0


def delete(db: Session, project: FormProject) -> None:
    db.delete(project)
    db.flush()
