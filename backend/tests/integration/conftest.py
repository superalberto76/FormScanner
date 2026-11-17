# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fixtures for DB-backed MATRIX integration tests.

These need a REAL Postgres test database — the models use ``postgresql.UUID`` and
native enum types, so SQLite is not an option. Point at a throwaway DB with
``TEST_DATABASE_URL`` and run from ``backend/``::

    TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/formscanner_test \
        pytest tests/integration

When ``TEST_DATABASE_URL`` is unset the whole integration suite is skipped, so the
pure unit tests (``tests/test_matrix_selection.py``) still run anywhere.

Isolation: each test runs inside an outer transaction joined via
``join_transaction_mode="create_savepoint"``, so the service's own ``db.commit()``
only releases a SAVEPOINT and the outer ``rollback()`` discards everything — no
data leaks between tests and the DB is left untouched.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

# Importing app.models registers every table on Base.metadata (needed for
# create_all and for the FK graph behind a matrix object).
import app.models  # noqa: F401
from app.models.base import Base
from app.models.enums import FormProjectType, FormVariantStatus, ProjectRole, UserRole
from app.models.form_project import FormProject
from app.models.form_project_member import FormProjectMember
from app.models.form_variant import FormVariant
from app.models.form_variant_page import FormVariantPage
from app.models.user import User


@pytest.fixture(scope="session")
def engine():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("set TEST_DATABASE_URL to run MATRIX integration tests")
    eng = create_engine(url, future=True)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    """A Session whose writes are rolled back after each test."""
    connection = engine.connect()
    trans = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        connection.close()


@pytest.fixture
def user(db) -> User:
    u = User(
        email=f"matrix-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=UserRole.USER,
    )
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def page(db, user) -> FormVariantPage:
    """A DRAFT page owned by ``user``, ready to host a MATRIX object."""
    project = FormProject(
        owner_id=user.id,
        type=FormProjectType.EXAM,
        name="Integration test project",
    )
    db.add(project)
    db.flush()
    # Membership is the source of truth for project access, so the
    # owner needs an explicit OWNER row for ``require_project_member`` to pass.
    db.add(
        FormProjectMember(
            project_id=project.id, user_id=user.id, role=ProjectRole.OWNER
        )
    )
    db.flush()
    variant = FormVariant(
        form_project_id=project.id,
        root_key="root",
        name="v1",
        status=FormVariantStatus.DRAFT,
        version_number=1,
        created_by_user_id=user.id,
    )
    db.add(variant)
    db.flush()
    pg = FormVariantPage(form_variant_id=variant.id, page_index=0)
    db.add(pg)
    db.flush()
    return pg
