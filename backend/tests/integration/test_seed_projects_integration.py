# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Seeding the COLL-* projects into an environment.

``seed`` builds COLL-MULTI, COLL-SINGLE and COLL-CONFIG for two EXISTING
users; every precondition that fails stops it before anything is written.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.models.enums import FormProjectStatus, FormVariantStatus, ProjectRole, UserRole
from app.models.form_project import FormProject
from app.models.form_project_member import FormProjectMember
from app.models.user import User
from tests.synthetic.seed_projects import (
    SPECS,
    SeedError,
    check_alembic_head,
    code_heads,
    find_users,
    seed,
)


def _user(db, **fields) -> User:
    u = User(
        email=f"seed-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.USER,
        **fields,
    )
    db.add(u)
    db.flush()
    return u


def _projects_of(db, owner: User) -> list[FormProject]:
    return list(db.scalars(select(FormProject).where(FormProject.owner_id == owner.id)))


def test_seed_builds_the_three_projects(db):
    owner, collaborator = _user(db), _user(db)

    projects = seed(db, owner, collaborator)

    assert [p.code for p in projects] == ["COLL-MULTI", "COLL-SINGLE", "COLL-CONFIG"]
    rows = {p.name: p for p in _projects_of(db, owner)}
    assert set(rows) == {spec.name for spec in SPECS}
    for project in projects:
        row = db.get(FormProject, project.project_id)
        assert row.owner_id == owner.id
        assert row.status is FormProjectStatus.ACTIVE
        assert (project.owner_id, project.collaborator_id) == (owner.id, collaborator.id)
        member = db.scalars(
            select(FormProjectMember).where(
                FormProjectMember.project_id == project.project_id,
                FormProjectMember.user_id == collaborator.id,
            )
        ).one()
        assert member.role is ProjectRole.COLLABORATOR
    multi = projects[0]
    assert {k: (v.status, v.vid_value) for k, v in multi.variants.items()} == {
        "A": (FormVariantStatus.PUBLISHED, "COLL-MULTI-A"),
        "B": (FormVariantStatus.PUBLISHED, "COLL-MULTI-B"),
        "C": (FormVariantStatus.PUBLISHED, "COLL-MULTI-C"),
        "D": (FormVariantStatus.DRAFT, "COLL-MULTI-D"),
    }


def test_seed_twice_is_refused_without_writing(db):
    owner, collaborator = _user(db), _user(db)
    seed(db, owner, collaborator)
    before = db.scalar(select(func.count()).select_from(FormProject))

    with pytest.raises(SeedError, match="already has"):
        seed(db, owner, collaborator)

    assert db.scalar(select(func.count()).select_from(FormProject)) == before


def test_same_names_of_another_owner_do_not_block(db):
    """Only the owner's own projects count: another user's COLL-* are not his."""
    seed(db, _user(db), _user(db))
    owner, collaborator = _user(db), _user(db)

    assert len(seed(db, owner, collaborator)) == 3


def test_find_users(db):
    owner, collaborator = _user(db), _user(db)
    assert find_users(db, owner.email, collaborator.email) == (owner, collaborator)

    with pytest.raises(SeedError, match="no user"):
        find_users(db, owner.email, "nobody@example.com")
    with pytest.raises(SeedError, match="two different users"):
        find_users(db, owner.email, owner.email)
    disabled = _user(db, disabled=True)
    with pytest.raises(SeedError, match="disabled"):
        find_users(db, disabled.email, collaborator.email)


def test_alembic_head(db):
    """The test database is migrated to head; another head stops."""
    connection = db.connection()
    check_alembic_head(connection)
    assert code_heads()

    with pytest.raises(SeedError, match="apply the migrations"):
        check_alembic_head(connection, heads={"0000000000ff"})
