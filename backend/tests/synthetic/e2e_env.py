# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Prepare the database and the kit of the browser end-to-end tests.

    cd backend
    python -m tests.synthetic.e2e_env --database-url URL --kit-out DIR \\
        --owner OWNER_EMAIL --collaborator COLLABORATOR_EMAIL --password PASSWORD

Playwright's global setup (``frontend/e2e/global-setup.ts``) runs it before
every e2e run, so every run starts from a known state:

  1. creates the e2e database if it does not exist, then EMPTIES it (drops and
     recreates the ``public`` schema) and brings it to the Alembic head of this
     code;
  2. creates the two operators, "Coll Owner" and "Coll Collaborator", with the
     given password;
  3. exports the test kit into DIR (``export_kit``: its COLL-* projects are
     built in a transaction that is rolled back) — the very bytes the
     synthetic scenarios upload;
  4. creates COLL-MULTI, COLL-SINGLE and COLL-CONFIG owned by the first
     operator, the second a member (``seed_projects``), and COMMITS.

It only ever touches a database whose name ends in ``_e2e``: it empties it,
so it refuses the app's and pytest's databases. ``--kit-out`` may be omitted
(step 3 skipped): the integration test does so, the export takes ~80 s.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

import app.models  # noqa: F401  (registers every mapper)
from app.core.security import hash_password
from app.models.enums import UserRole
from app.models.user import User
from tests.synthetic import export_kit as kit
from tests.synthetic.builders import (
    COLLABORATOR_LAST_NAME,
    OWNER_LAST_NAME,
    USER_FIRST_NAME,
)
from tests.synthetic.seed_projects import ALEMBIC_INI, SeedError, check_alembic_head, seed

E2E_SUFFIX = "_e2e"
MAINTENANCE_DATABASE = "postgres"


class E2eEnvError(Exception):
    """A precondition failed: the database was not touched."""


def check_e2e_url(url: str) -> str:
    """The database name of *url*, which must end in ``_e2e``."""
    name = make_url(url).database or ""
    if not name.endswith(E2E_SUFFIX):
        raise E2eEnvError(
            f"the database {name!r} does not end in {E2E_SUFFIX!r}: e2e_env would empty it, "
            "so it only works on a database dedicated to the e2e tests (e.g. formscanner_e2e)"
        )
    return name


def ensure_database(url: str) -> bool:
    """Create the database of *url* if missing; True when it was created."""
    name = check_e2e_url(url)
    maintenance = make_url(url).set(database=MAINTENANCE_DATABASE)
    engine = create_engine(maintenance, isolation_level="AUTOCOMMIT", future=True)
    try:
        with engine.connect() as connection:
            exists = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
            ).first()
            if exists:
                return False
            # The name is checked above and quoted: CREATE DATABASE takes no parameter.
            connection.execute(text(f'CREATE DATABASE "{name}"'))
            return True
    finally:
        engine.dispose()


def empty_database(url: str) -> None:
    """Drop every table, type and Alembic version: the ``public`` schema anew."""
    check_e2e_url(url)
    engine = create_engine(url, future=True)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()


def upgrade_head(url: str) -> None:
    """``alembic upgrade head`` on *url*, in a child process.

    ``alembic/env.py`` reads the URL from ``DATABASE_URL``: the child gets the
    e2e one, whatever the caller's environment holds."""
    check_e2e_url(url)
    env = {**os.environ, "DATABASE_URL": url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), "upgrade", "head"],
        cwd=ALEMBIC_INI.parent,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise E2eEnvError(f"alembic upgrade head failed:\n{result.stderr}")


def create_operators(
    db: Session, owner_email: str, collaborator_email: str, password: str
) -> tuple[User, User]:
    """The two operators of the e2e tests, named as in the synthetic tests."""
    if owner_email == collaborator_email:
        raise E2eEnvError("owner and collaborator must have different emails")
    password_hash = hash_password(password)
    users = []
    for email, last_name in (
        (owner_email, OWNER_LAST_NAME),
        (collaborator_email, COLLABORATOR_LAST_NAME),
    ):
        user = User(
            email=email,
            password_hash=password_hash,
            role=UserRole.USER,
            first_name=USER_FIRST_NAME,
            last_name=last_name,
        )
        db.add(user)
        users.append(user)
    db.flush()
    owner, collaborator = users
    return owner, collaborator


def export_kit_to(url: str, out: Path) -> kit.KitSummary:
    """The test kit into *out*, its projects built in a rolled-back transaction."""
    kit.check_out_dir(out)
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            session = Session(bind=connection, join_transaction_mode="create_savepoint")
            try:
                return kit.export_kit(session, out)
            finally:
                session.close()
                transaction.rollback()  # never a commit: the kit leaves nothing in the DB
    finally:
        engine.dispose()


def seed_operators_and_projects(
    url: str, owner_email: str, collaborator_email: str, password: str
) -> list[str]:
    """Operators and COLL-* projects in ONE committed transaction; their names."""
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            check_alembic_head(connection)
            # The services commit as they go: within the outer transaction
            # those commits only release a SAVEPOINT (as in seed_projects).
            session = Session(bind=connection, join_transaction_mode="create_savepoint")
            try:
                owner, collaborator = create_operators(
                    session, owner_email, collaborator_email, password
                )
                projects = seed(session, owner, collaborator)
                names = [project.spec.name for project in projects]
            except BaseException:
                session.close()
                transaction.rollback()
                raise
            session.close()
            transaction.commit()
            return names
    finally:
        engine.dispose()


def prepare(
    url: str,
    owner_email: str,
    collaborator_email: str,
    password: str,
    kit_out: Path | None,
    log=print,
) -> None:
    """Steps 1–4 of the module docstring (step 3 only with *kit_out*)."""
    check_e2e_url(url)
    started = time.monotonic()

    def step(message: str) -> None:
        log(f"[e2e_env {time.monotonic() - started:5.1f}s] {message}")

    created = ensure_database(url)
    empty_database(url)
    upgrade_head(url)
    step(f"database {'created, ' if created else ''}emptied and brought to the Alembic head")
    if kit_out is not None:
        summary = export_kit_to(url, kit_out)
        step(f"kit exported to {kit_out}: {len(summary.files)} files")
    names = seed_operators_and_projects(url, owner_email, collaborator_email, password)
    step(f"operators {owner_email}, {collaborator_email}; projects {', '.join(names)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tests.synthetic.e2e_env",
        description="Prepare the database and kit of the browser e2e tests (empties the *_e2e database).",
    )
    parser.add_argument("--database-url", required=True, help="e2e database, its name ending in _e2e")
    parser.add_argument("--kit-out", type=Path, help="kit folder, empty or new (omitted: no kit)")
    parser.add_argument("--owner", required=True, help="email of the first operator (the owner)")
    parser.add_argument("--collaborator", required=True, help="email of the second operator")
    parser.add_argument("--password", required=True, help="password of the two operators")
    args = parser.parse_args(argv)
    try:
        prepare(args.database_url, args.owner, args.collaborator, args.password, args.kit_out)
    except (E2eEnvError, SeedError) as exc:
        print(f"e2e_env: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
