# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for member management + user search.

Covers the owner-only management invariants (≥1 owner, never act on yourself,
no duplicates, existing users only), the owner/admin user-search gate and its
filters, and the batched ``members_preview``/``member_count`` on the project
list (proving no N+1 on the members table).

Reuses the shared ``db`` fixture (conftest.py) — requires TEST_DATABASE_URL, so
the whole module is skipped without a real Postgres.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import event

from app.models.enums import FormProjectType, ProjectRole, UserRole
from app.models.form_project_member import FormProjectMember
from app.models.user import User
from app.repositories import form_project_member_repo
from app.schemas.form_project import FormProjectCreate
from app.services import form_project_service, user_service


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_user(
    db,
    *,
    role: UserRole = UserRole.USER,
    first_name: str | None = None,
    last_name: str | None = None,
    email: str | None = None,
    disabled: bool = False,
) -> User:
    u = User(
        email=email or f"m77p2-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=role,
        first_name=first_name,
        last_name=last_name,
        disabled=disabled,
    )
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def owner(db) -> User:
    return _make_user(db)


@pytest.fixture
def other(db) -> User:
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


def _second_owner(db, project, user) -> FormProjectMember:
    m = FormProjectMember(
        project_id=project.id, user_id=user.id, role=ProjectRole.OWNER
    )
    db.add(m)
    db.flush()
    return m


# ---------------------------------------------------------------------------
# add_collaborator
# ---------------------------------------------------------------------------


def test_add_collaborator_ok(db, project, owner, other):
    read = form_project_service.add_collaborator(db, project.id, other.id, owner)
    assert read.user_id == other.id
    assert read.role is ProjectRole.COLLABORATOR
    # The new member now has access.
    assert form_project_member_repo.get(db, project.id, other.id) is not None


def test_add_collaborator_duplicate_409(db, project, owner, other):
    form_project_service.add_collaborator(db, project.id, other.id, owner)
    with pytest.raises(HTTPException) as ei:
        form_project_service.add_collaborator(db, project.id, other.id, owner)
    assert ei.value.status_code == 409


def test_add_collaborator_unknown_user_404(db, project, owner):
    with pytest.raises(HTTPException) as ei:
        form_project_service.add_collaborator(db, project.id, uuid.uuid4(), owner)
    assert ei.value.status_code == 404


def test_add_collaborator_non_owner_403(db, project, owner, other, stranger):
    # A collaborator may not add members.
    form_project_service.add_collaborator(db, project.id, other.id, owner)
    with pytest.raises(HTTPException) as ei:
        form_project_service.add_collaborator(db, project.id, stranger.id, other)
    assert ei.value.status_code == 403


# ---------------------------------------------------------------------------
# remove_member
# ---------------------------------------------------------------------------


def test_remove_collaborator_ok_loses_access(db, project, owner, other):
    form_project_service.add_collaborator(db, project.id, other.id, owner)
    form_project_service.remove_member(db, project.id, other.id, owner)

    assert form_project_member_repo.get(db, project.id, other.id) is None
    # require_project_member now rejects them; project gone from their list.
    with pytest.raises(HTTPException) as ei:
        form_project_service.require_project_member(db, project.id, other)
    assert ei.value.status_code == 403
    assert form_project_service.list_projects(db, other) == []


def test_remove_self_as_sole_owner_is_400_self_rule_first(db, project, owner):
    # Owner == self == last owner: the self-rule is checked first → 400, not 409.
    with pytest.raises(HTTPException) as ei:
        form_project_service.remove_member(db, project.id, owner.id, owner)
    assert ei.value.status_code == 400


def test_remove_self_owner_400_even_with_other_owner(db, project, owner, other):
    _second_owner(db, project, other)  # now two owners
    with pytest.raises(HTTPException) as ei:
        form_project_service.remove_member(db, project.id, owner.id, owner)
    assert ei.value.status_code == 400


def test_remove_other_last_owner_409(db, other, admin):
    # `other` is the SOLE owner of a fresh project; an admin tries to remove them
    # → 409 (a project must keep at least one owner).
    proj = form_project_service.create_project(
        db, other, FormProjectCreate(type=FormProjectType.EXAM, name="solo")
    )
    with pytest.raises(HTTPException) as ei:
        form_project_service.remove_member(db, proj.id, other.id, admin)
    assert ei.value.status_code == 409


def test_remove_unknown_member_404(db, project, owner, stranger):
    with pytest.raises(HTTPException) as ei:
        form_project_service.remove_member(db, project.id, stranger.id, owner)
    assert ei.value.status_code == 404


# ---------------------------------------------------------------------------
# set_member_role
# ---------------------------------------------------------------------------


def test_promote_collaborator_to_owner(db, project, owner, other):
    form_project_service.add_collaborator(db, project.id, other.id, owner)
    read = form_project_service.set_member_role(
        db, project.id, other.id, ProjectRole.OWNER, owner
    )
    assert read.role is ProjectRole.OWNER
    assert form_project_member_repo.count_owners(db, project.id) == 2


def test_demote_last_owner_409(db, project, owner):
    # Demote the only owner via an admin actor (avoids the self-rule), to isolate
    # the last-owner guard → 409.
    admin = _make_user(db, role=UserRole.ADMIN)
    with pytest.raises(HTTPException) as ei:
        form_project_service.set_member_role(
            db, project.id, owner.id, ProjectRole.COLLABORATOR, admin
        )
    assert ei.value.status_code == 409


def test_demote_yourself_400(db, project, owner, other):
    _second_owner(db, project, other)  # two owners, so last-owner guard is clear
    with pytest.raises(HTTPException) as ei:
        form_project_service.set_member_role(
            db, project.id, owner.id, ProjectRole.COLLABORATOR, owner
        )
    assert ei.value.status_code == 400


def test_set_role_noop_returns_current(db, project, owner, other):
    form_project_service.add_collaborator(db, project.id, other.id, owner)
    read = form_project_service.set_member_role(
        db, project.id, other.id, ProjectRole.COLLABORATOR, owner
    )
    assert read.role is ProjectRole.COLLABORATOR


def test_set_role_non_owner_actor_403(db, project, owner, other, stranger):
    form_project_service.add_collaborator(db, project.id, other.id, owner)
    with pytest.raises(HTTPException) as ei:
        form_project_service.set_member_role(
            db, project.id, other.id, ProjectRole.OWNER, stranger
        )
    assert ei.value.status_code == 403


# ---------------------------------------------------------------------------
# user search
# ---------------------------------------------------------------------------


def test_search_owner_actor_ok_finds_by_name(db, project, owner):
    _make_user(db, first_name="Alice", last_name="Verdi",
               email="alice.verdi@example.com")
    hits = user_service.search_users(db, "Verdi", None, owner)
    assert any(h.last_name == "Verdi" for h in hits)


def test_search_non_owner_non_admin_403(db, stranger):
    with pytest.raises(HTTPException) as ei:
        user_service.search_users(db, "anything", None, stranger)
    assert ei.value.status_code == 403


def test_search_admin_bypass(db, admin):
    _make_user(db, first_name="Bob", last_name="Rossi", email="bob.rossi@example.com")
    hits = user_service.search_users(db, "Rossi", None, admin)
    assert any(h.last_name == "Rossi" for h in hits)


def test_search_short_query_returns_empty(db, project, owner):
    assert user_service.search_users(db, "a", None, owner) == []
    assert user_service.search_users(db, " ", None, owner) == []


def test_search_excludes_current_members_and_disabled(db, project, owner):
    member = _make_user(db, first_name="Carla", last_name="Neri",
                        email="carla.neri@example.com")
    form_project_service.add_collaborator(db, project.id, member.id, owner)
    _make_user(db, first_name="Dario", last_name="Neri",
               email="dario.neri@example.com", disabled=True)

    # Without exclusion, the active "Neri" appears but the disabled one never does.
    plain = {h.email for h in user_service.search_users(db, "Neri", None, owner)}
    assert "carla.neri@example.com" in plain
    assert "dario.neri@example.com" not in plain

    # With exclude_project_id, the current member is filtered out too.
    excluded = {
        h.email
        for h in user_service.search_users(db, "Neri", project.id, owner)
    }
    assert "carla.neri@example.com" not in excluded
    assert "dario.neri@example.com" not in excluded


# ---------------------------------------------------------------------------
# list_projects — members_preview (≤3, owners first) + member_count, batched
# ---------------------------------------------------------------------------


def test_list_projects_members_preview_owners_first_and_count(db, owner):
    project = form_project_service.create_project(
        db, owner, FormProjectCreate(type=FormProjectType.EXAM, name="stack")
    )
    # Add 4 collaborators (total 5 members: 1 owner + 4 collaborators).
    for i in range(4):
        u = _make_user(db, first_name=f"C{i}", last_name="X")
        form_project_service.add_collaborator(db, project.id, u.id, owner)

    items = form_project_service.list_projects(db, owner)
    item = next(i for i in items if i.id == project.id)

    assert item.member_count == 5
    assert len(item.members_preview) == 3  # capped at 3
    # Owner sorts first in the preview.
    assert item.members_preview[0].user_id == owner.id
    assert item.members_preview[0].role is ProjectRole.OWNER


def _count_member_table_queries(db, fn):
    """Run *fn* and count SQL statements touching form_project_members."""
    bind = db.get_bind()
    seen: list[str] = []

    def _capture(conn, cursor, statement, params, context, executemany):
        if "form_project_members" in statement:
            seen.append(statement)

    event.listen(bind, "before_cursor_execute", _capture)
    try:
        fn()
    finally:
        event.remove(bind, "before_cursor_execute", _capture)
    return len(seen)


def test_list_projects_no_n_plus_one_on_members(db, owner):
    # Two projects vs four projects: the number of member-table queries must be
    # CONSTANT (batched), not grow with the project count.
    for n in range(2):
        form_project_service.create_project(
            db, owner, FormProjectCreate(type=FormProjectType.EXAM, name=f"a{n}")
        )
    q_two = _count_member_table_queries(
        db, lambda: form_project_service.list_projects(db, owner)
    )

    for n in range(2):
        form_project_service.create_project(
            db, owner, FormProjectCreate(type=FormProjectType.EXAM, name=f"b{n}")
        )
    q_four = _count_member_table_queries(
        db, lambda: form_project_service.list_projects(db, owner)
    )

    assert q_two == q_four  # no N+1: independent of the number of projects
