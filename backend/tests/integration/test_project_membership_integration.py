# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for role-based project membership.

Membership (``FormProjectMember``) is the source of truth for project access.
These service-level tests assert the two authz primitives, the OWNER-seeding on
create, membership-based list visibility + ``is_owner``, collaborator access to
variants/export/authoring, and the read-only members roster.

Reuses the shared ``db`` fixture (conftest.py) — requires TEST_DATABASE_URL, so
the whole module is skipped without a real Postgres.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.models.enums import FormProjectType, ProjectRole, UserRole
from app.models.form_project_member import FormProjectMember
from app.models.user import User
from app.repositories import form_project_member_repo
from app.schemas.form_project import FormProjectCreate
from app.schemas.form_variant import FormVariantCreate
from app.services import (
    form_project_service,
    form_variant_export_service,
    form_variant_service,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_user(db, role: UserRole = UserRole.USER) -> User:
    u = User(
        email=f"m77-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=role,
    )
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def owner(db) -> User:
    return _make_user(db)


@pytest.fixture
def collaborator(db) -> User:
    return _make_user(db)


@pytest.fixture
def stranger(db) -> User:
    return _make_user(db)


@pytest.fixture
def admin(db) -> User:
    return _make_user(db, role=UserRole.ADMIN)


@pytest.fixture
def project(db, owner):
    """A project created by ``owner`` — create_project seeds the OWNER member."""
    return form_project_service.create_project(
        db, owner, FormProjectCreate(type=FormProjectType.EXAM, name="Members")
    )


def _add_collaborator(db, project, user) -> FormProjectMember:
    member = FormProjectMember(
        project_id=project.id, user_id=user.id, role=ProjectRole.COLLABORATOR
    )
    db.add(member)
    db.flush()
    return member


# ---------------------------------------------------------------------------
# create_project seeds the creator as OWNER
# ---------------------------------------------------------------------------


def test_create_project_seeds_creator_as_owner(db, owner, project):
    member = form_project_member_repo.get(db, project.id, owner.id)
    assert member is not None
    assert member.role is ProjectRole.OWNER
    assert form_project_member_repo.count_owners(db, project.id) == 1
    # owner_id is kept as the primary/creator marker.
    assert project.owner_id == owner.id


# ---------------------------------------------------------------------------
# require_project_member
# ---------------------------------------------------------------------------


def test_member_primitive_allows_owner(db, project, owner):
    got = form_project_service.require_project_member(db, project.id, owner)
    assert got.id == project.id


def test_member_primitive_allows_collaborator(db, project, collaborator):
    _add_collaborator(db, project, collaborator)
    got = form_project_service.require_project_member(db, project.id, collaborator)
    assert got.id == project.id


def test_member_primitive_rejects_non_member(db, project, stranger):
    with pytest.raises(HTTPException) as ei:
        form_project_service.require_project_member(db, project.id, stranger)
    assert ei.value.status_code == 403


def test_member_primitive_missing_project_404(db, owner):
    with pytest.raises(HTTPException) as ei:
        form_project_service.require_project_member(db, uuid.uuid4(), owner)
    assert ei.value.status_code == 404


def test_member_primitive_admin_bypass(db, project, admin):
    # admin is NOT a member, yet bypasses the gate.
    got = form_project_service.require_project_member(db, project.id, admin)
    assert got.id == project.id


# ---------------------------------------------------------------------------
# require_project_owner
# ---------------------------------------------------------------------------


def test_owner_primitive_allows_owner(db, project, owner):
    got = form_project_service.require_project_owner(db, project.id, owner)
    assert got.id == project.id


def test_owner_primitive_rejects_collaborator(db, project, collaborator):
    _add_collaborator(db, project, collaborator)
    with pytest.raises(HTTPException) as ei:
        form_project_service.require_project_owner(db, project.id, collaborator)
    assert ei.value.status_code == 403


def test_owner_primitive_admin_bypass(db, project, admin):
    got = form_project_service.require_project_owner(db, project.id, admin)
    assert got.id == project.id


# ---------------------------------------------------------------------------
# list_projects visibility + is_owner
# ---------------------------------------------------------------------------


def test_list_projects_collaborator_sees_shared_with_is_owner_flag(
    db, project, owner, collaborator
):
    _add_collaborator(db, project, collaborator)

    owner_list = form_project_service.list_projects(db, owner)
    assert [p.id for p in owner_list] == [project.id]
    assert owner_list[0].is_owner is True

    collab_list = form_project_service.list_projects(db, collaborator)
    assert [p.id for p in collab_list] == [project.id]
    assert collab_list[0].is_owner is False


def test_list_projects_non_member_sees_nothing(db, project, stranger):
    assert form_project_service.list_projects(db, stranger) == []


def test_list_projects_admin_sees_all_but_is_owner_false(db, project, admin):
    items = form_project_service.list_projects(db, admin)
    by_id = {p.id: p for p in items}
    assert project.id in by_id
    # admin is not a member of this project → is_owner reflects that.
    assert by_id[project.id].is_owner is False


# ---------------------------------------------------------------------------
# Variant access through a collaborator (MEMBER-level)
# ---------------------------------------------------------------------------


def test_collaborator_has_full_variant_access(db, project, collaborator):
    _add_collaborator(db, project, collaborator)

    # Create a DRAFT variant as the collaborator.
    variant = form_variant_service.create_variant(
        db, project.id, FormVariantCreate(name="V1"), collaborator
    )
    # View its detail.
    detail = form_variant_service.get_variant_detail(db, variant.id, collaborator)
    assert detail.id == variant.id
    # Export path (preview metadata exercises the export authorization gate).
    meta = form_variant_export_service.get_preview_metadata(
        db, variant.id, collaborator
    )
    assert meta["variant_id"] == str(variant.id)
    # Delete the DRAFT variant.
    form_variant_service.delete_variant(db, variant.id, collaborator)
    with pytest.raises(HTTPException) as ei:
        form_variant_service.get_variant_detail(db, variant.id, collaborator)
    assert ei.value.status_code == 404


def test_non_member_variant_access_rejected(db, project, owner, stranger):
    variant = form_variant_service.create_variant(
        db, project.id, FormVariantCreate(name="V1"), owner
    )
    with pytest.raises(HTTPException) as ei:
        form_variant_service.get_variant_detail(db, variant.id, stranger)
    assert ei.value.status_code == 403
    with pytest.raises(HTTPException) as ei2:
        form_variant_export_service.get_preview_metadata(db, variant.id, stranger)
    assert ei2.value.status_code == 403


# ---------------------------------------------------------------------------
# Members roster (read-only)
# ---------------------------------------------------------------------------


def test_list_members_returns_roster_owners_first(db, project, owner, collaborator):
    _add_collaborator(db, project, collaborator)

    members = form_project_service.list_members(db, project.id, owner)
    assert [m.role for m in members] == [
        ProjectRole.OWNER,
        ProjectRole.COLLABORATOR,
    ]
    assert members[0].user_id == owner.id
    assert members[1].user_id == collaborator.id
    # No avatar uploaded → has_avatar is False (wired to the real column).
    assert members[0].has_avatar is False


def test_list_members_collaborator_may_view(db, project, collaborator):
    _add_collaborator(db, project, collaborator)
    members = form_project_service.list_members(db, project.id, collaborator)
    assert {m.user_id for m in members} == {project.owner_id, collaborator.id}


def test_list_members_non_member_rejected(db, project, stranger):
    with pytest.raises(HTTPException) as ei:
        form_project_service.list_members(db, project.id, stranger)
    assert ei.value.status_code == 403
