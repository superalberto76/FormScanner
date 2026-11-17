# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for the admin user listing.

Covers ``user_service.list_users`` / ``user_repo.search_paginated``: pagination
(page slices + correct total), case-insensitive search across first/last/email,
and the rule that the list shows EVERYONE — admins and disabled users included
(unlike the collaborator picker ``search_users``).

Requires ``TEST_DATABASE_URL`` — the ``db`` fixture is shared via conftest.py.
Each created user carries a unique per-test *token* in its searchable fields, so
queries are scoped to that test's rows and totals stay deterministic regardless
of any other rows in the throwaway DB.
"""
from __future__ import annotations

import uuid

import pytest

from app.models.enums import UserRole
from app.models.user import User
from app.services import user_service


@pytest.fixture
def admin(db) -> User:
    a = User(
        email=f"admin-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.ADMIN,
    )
    db.add(a)
    db.flush()
    return a


def _mk(
    db,
    token: str,
    *,
    first: str = "X",
    last: str = "X",
    role: UserRole = UserRole.USER,
    disabled: bool = False,
) -> User:
    """Create + flush a user whose email embeds *token* (so ``q=token`` finds it)."""
    u = User(
        email=f"{uuid.uuid4().hex}-{token}@example.com",
        password_hash="x",
        first_name=first,
        last_name=last,
        role=role,
        disabled=disabled,
    )
    db.add(u)
    db.flush()
    return u


def test_lists_users_paginated_with_correct_total(db, admin):
    token = uuid.uuid4().hex[:8]
    # last_name = Last0..Last4 → the order-by gives a stable, predictable page order.
    for i in range(5):
        _mk(db, token, first=f"First{i}", last=f"Last{i}")

    p0 = user_service.list_users(db, q=token, limit=2, offset=0, actor=admin)
    assert p0.total == 5
    assert len(p0.items) == 2
    assert (p0.limit, p0.offset) == (2, 0)

    p1 = user_service.list_users(db, q=token, limit=2, offset=2, actor=admin)
    assert p1.total == 5
    assert len(p1.items) == 2

    p2 = user_service.list_users(db, q=token, limit=2, offset=4, actor=admin)
    assert p2.total == 5
    assert len(p2.items) == 1  # last, partial page — offset honored

    # Pages are disjoint and together cover exactly the 5 created rows.
    ids = [i.id for i in (*p0.items, *p1.items, *p2.items)]
    assert len(ids) == len(set(ids)) == 5


def test_q_filters_case_insensitively_on_first_last_or_email(db, admin):
    token = uuid.uuid4().hex[:8]
    by_first = _mk(db, token, first=f"Alessandro{token}", last="X")
    by_last = _mk(db, token, first="X", last=f"Bianchi{token}")

    # ILIKE on first name, queried in a different case than stored.
    r_first = user_service.list_users(
        db, q=f"alessandro{token}".upper(), limit=50, offset=0, actor=admin
    )
    ids_first = {i.id for i in r_first.items}
    assert by_first.id in ids_first
    assert by_last.id not in ids_first  # the fragment matches only the first row

    # ILIKE on last name.
    r_last = user_service.list_users(
        db, q=f"bianchi{token}", limit=50, offset=0, actor=admin
    )
    assert by_last.id in {i.id for i in r_last.items}

    # ILIKE on email (the shared token lives in every email) finds both rows.
    r_email = user_service.list_users(db, q=token, limit=50, offset=0, actor=admin)
    ids_email = {i.id for i in r_email.items}
    assert {by_first.id, by_last.id}.issubset(ids_email)
    assert r_email.total == 2


def test_blank_query_does_not_filter(db, admin):
    token = uuid.uuid4().hex[:8]
    a = _mk(db, token)
    b = _mk(db, token)

    # A whitespace-only q must behave like "no filter" (lists everyone).
    r = user_service.list_users(db, q="   ", limit=100, offset=0, actor=admin)
    ids = {i.id for i in r.items}
    assert {a.id, b.id}.issubset(ids)
    assert r.total >= 2


def test_list_includes_admins_and_disabled_users(db, admin):
    token = uuid.uuid4().hex[:8]
    disabled = _mk(db, token, disabled=True)
    other_admin = _mk(db, token, role=UserRole.ADMIN)

    r = user_service.list_users(db, q=token, limit=50, offset=0, actor=admin)
    by_id = {i.id: i for i in r.items}

    # Both appear — the admin screen lists everyone.
    assert disabled.id in by_id
    assert other_admin.id in by_id
    # role / disabled survive the UserListItem mapping intact.
    assert by_id[disabled.id].disabled is True
    assert by_id[other_admin.id].role is UserRole.ADMIN
