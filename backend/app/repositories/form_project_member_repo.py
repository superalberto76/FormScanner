# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Data-access layer for FormProjectMember.

Membership rows are the source of truth for project access. This module is a
thin query layer; authorization decisions live in ``form_project_service``.
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.enums import ProjectRole
from app.models.form_project_member import FormProjectMember
from app.models.user import User


def add(db: Session, member: FormProjectMember) -> FormProjectMember:
    db.add(member)
    db.flush()  # populate member.id
    return member


def delete(db: Session, member: FormProjectMember) -> None:
    db.delete(member)
    db.flush()


def get(
    db: Session, project_id: uuid.UUID, user_id: uuid.UUID
) -> FormProjectMember | None:
    """The membership row for (project, user), or None if not a member."""
    stmt = select(FormProjectMember).where(
        FormProjectMember.project_id == project_id,
        FormProjectMember.user_id == user_id,
    )
    return db.scalars(stmt).first()


def list_for_project(
    db: Session, project_id: uuid.UUID
) -> list[tuple[FormProjectMember, User]]:
    """Members of *project_id* joined to their User, OWNERS first then oldest.

    Ordered so the creator/owner sorts first for the avatar stack: OWNER rows
    (role ascending puts 'COLLABORATOR' after 'OWNER' alphabetically, but we sort
    explicitly on an OWNER-first flag to stay robust to enum value spelling),
    then by ``created_at`` ascending within a role.
    """
    owner_first = (FormProjectMember.role != ProjectRole.OWNER)
    stmt = (
        select(FormProjectMember, User)
        .join(User, User.id == FormProjectMember.user_id)
        .where(FormProjectMember.project_id == project_id)
        .order_by(owner_first, FormProjectMember.created_at.asc())
    )
    return [(m, u) for m, u in db.execute(stmt).all()]


def list_for_projects(
    db: Session, project_ids: list[uuid.UUID]
) -> list[tuple[FormProjectMember, User]]:
    """Members of ALL the given projects joined to User, in ONE query.

    Ordered by project, then OWNERS-first, then oldest — so the caller can group
    by ``project_id`` and slice the first N for an owners-first avatar preview
    without an extra query per project (no N+1).
    """
    if not project_ids:
        return []
    owner_first = (FormProjectMember.role != ProjectRole.OWNER)
    stmt = (
        select(FormProjectMember, User)
        .join(User, User.id == FormProjectMember.user_id)
        .where(FormProjectMember.project_id.in_(project_ids))
        .order_by(
            FormProjectMember.project_id,
            owner_first,
            FormProjectMember.created_at.asc(),
        )
    )
    return [(m, u) for m, u in db.execute(stmt).all()]


def member_user_ids(db: Session, project_id: uuid.UUID) -> set[uuid.UUID]:
    """The set of user ids already members of *project_id* (for search exclusion)."""
    stmt = select(FormProjectMember.user_id).where(
        FormProjectMember.project_id == project_id
    )
    return set(db.scalars(stmt).all())


def has_owner_membership(db: Session, user_id: uuid.UUID) -> bool:
    """True iff *user_id* is an OWNER of at least one project.

    Backs the ``require_any_project_owner`` gate for user-search.
    """
    stmt = (
        select(FormProjectMember.id)
        .where(
            FormProjectMember.user_id == user_id,
            FormProjectMember.role == ProjectRole.OWNER,
        )
        .limit(1)
    )
    return db.scalar(stmt) is not None


def count_owners(db: Session, project_id: uuid.UUID) -> int:
    """Number of OWNER members of *project_id* (the at-least-one-owner invariant)."""
    count = db.scalar(
        select(func.count(FormProjectMember.id)).where(
            FormProjectMember.project_id == project_id,
            FormProjectMember.role == ProjectRole.OWNER,
        )
    )
    return count or 0


def roles_for_user(
    db: Session, user_id: uuid.UUID, project_ids: list[uuid.UUID]
) -> dict[uuid.UUID, ProjectRole]:
    """Map project_id -> the caller's role, for the given projects.

    Only projects where *user_id* is a member appear in the result; used to
    compute ``is_owner`` in the project list.
    """
    if not project_ids:
        return {}
    stmt = select(
        FormProjectMember.project_id, FormProjectMember.role
    ).where(
        FormProjectMember.user_id == user_id,
        FormProjectMember.project_id.in_(project_ids),
    )
    return {pid: role for pid, role in db.execute(stmt).all()}
