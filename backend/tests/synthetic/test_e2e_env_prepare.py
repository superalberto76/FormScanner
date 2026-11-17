# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``tests.synthetic.e2e_env`` without the kit export.

Runs on its own database, ``<TEST_DATABASE_URL name>_e2e`` (created here and
dropped at the end): never the e2e one of Playwright, never pytest's.

Marked ``synthetic``: it creates and drops a database next to pytest's, so it
stays out of the default suite, like the e2e tests it serves::

    pytest -m synthetic tests/synthetic/test_e2e_env_prepare.py
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.security import verify_password
from app.models.enums import FormProjectStatus, ProjectRole
from app.models.form_project import FormProject
from app.models.form_project_member import FormProjectMember
from app.models.user import User
from tests.synthetic import e2e_env
from tests.synthetic.seed_projects import code_heads
from tests.synthetic.specs import COLL_CONFIG, COLL_MULTI, COLL_SINGLE

OWNER = "e2e-owner@example.com"
COLLABORATOR = "e2e-collab@example.com"
PASSWORD = "E2e-Password-1"

pytestmark = pytest.mark.synthetic


@pytest.fixture(scope="module")
def e2e_url():
    base = os.environ.get("TEST_DATABASE_URL")
    if not base:
        pytest.skip("set TEST_DATABASE_URL to run the e2e_env test")
    url = make_url(base)
    url = url.set(database=f"{url.database}{e2e_env.E2E_SUFFIX}")
    rendered = url.render_as_string(hide_password=False)
    yield rendered
    maintenance = url.set(database=e2e_env.MAINTENANCE_DATABASE)
    engine = create_engine(maintenance, isolation_level="AUTOCOMMIT", future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{url.database}"'))
    finally:
        engine.dispose()


def _state(url: str):
    engine = create_engine(url, future=True)
    try:
        with Session(engine) as db:
            users = {u.email: u for u in db.scalars(select(User)).all()}
            projects = db.scalars(select(FormProject)).all()
            members = db.execute(
                select(FormProjectMember.project_id, FormProjectMember.user_id, FormProjectMember.role)
            ).all()
            versions = set(db.execute(text("SELECT version_num FROM alembic_version")).scalars())
            return users, projects, members, versions
    finally:
        engine.dispose()


def test_refuses_a_database_not_ending_in_e2e():
    with pytest.raises(e2e_env.E2eEnvError):
        e2e_env.prepare(
            os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://u:p@h/formscanner_test"),
            OWNER, COLLABORATOR, PASSWORD, kit_out=None, log=lambda _: None,
        )


def test_refuses_the_same_email_for_both_operators():
    # Checked before any write: no session is touched.
    with pytest.raises(e2e_env.E2eEnvError):
        e2e_env.create_operators(None, OWNER, OWNER, PASSWORD)


def test_prepare_empties_migrates_and_seeds(e2e_url):
    # Twice: the second run starts from the first one's data and must leave
    # the same state (the DB is emptied, nothing is duplicated).
    for _ in range(2):
        e2e_env.prepare(e2e_url, OWNER, COLLABORATOR, PASSWORD, kit_out=None, log=lambda _: None)

    users, projects, members, versions = _state(e2e_url)

    assert versions == code_heads()
    assert set(users) == {OWNER, COLLABORATOR}
    owner, collaborator = users[OWNER], users[COLLABORATOR]
    assert (owner.first_name, owner.last_name) == ("Coll", "Owner")
    assert (collaborator.first_name, collaborator.last_name) == ("Coll", "Collaborator")
    for user in (owner, collaborator):
        assert verify_password(PASSWORD, user.password_hash)
        assert not user.disabled and not user.must_change_password

    assert sorted(p.name for p in projects) == sorted(
        s.name for s in (COLL_MULTI, COLL_SINGLE, COLL_CONFIG)
    )
    for project in projects:
        assert project.owner_id == owner.id
        assert project.status == FormProjectStatus.ACTIVE
        roles = {(m.user_id, m.role) for m in members if m.project_id == project.id}
        assert roles == {(owner.id, ProjectRole.OWNER), (collaborator.id, ProjectRole.COLLABORATOR)}
