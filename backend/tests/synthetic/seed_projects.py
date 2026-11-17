# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Create the COLL-* projects in an environment's database.

    cd backend
    python -m tests.synthetic.seed_projects --database-url URL \\
        --owner OWNER_EMAIL --collaborator COLLABORATOR_EMAIL

Builds COLL-MULTI, COLL-SINGLE and COLL-CONFIG from the same spec as the
synthetic tests, through the app's services (``builders.build_project``),
owned by OWNER with COLLABORATOR as a member, and COMMITS. Meant to run ONCE
per deployed environment: the kit of ``export_kit`` is recognised on these
projects because matching reads the VID value and the spec fixes it.

It writes nothing — and says why — when:
  * a user does not exist, is disabled, or both emails are the same user;
  * the database is not at the Alembic head of this code (the services would
    write with a schema they do not know);
  * the owner already has a project with one of the three names.

It never deletes or changes anything that exists. All three projects go in
one transaction: either all of them are created, or none.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

import app.models  # noqa: F401  (registers every mapper)
from app.models.form_project import FormProject
from app.models.user import User
from app.repositories import user_repo
from tests.synthetic.builders import CollProject, build_project
from tests.synthetic.specs import COLL_CONFIG, COLL_MULTI, COLL_SINGLE

SPECS = (COLL_MULTI, COLL_SINGLE, COLL_CONFIG)

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


class SeedError(Exception):
    """A precondition failed: nothing was written."""


def code_heads() -> set[str]:
    """The Alembic head(s) of this code."""
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_INI.parent / "alembic"))
    return set(ScriptDirectory.from_config(config).get_heads())


def check_alembic_head(connection: Connection, heads: set[str] | None = None) -> None:
    """The database must be exactly at the code's head."""
    heads = code_heads() if heads is None else heads
    try:
        versions = set(connection.execute(text("SELECT version_num FROM alembic_version")).scalars())
    except Exception as exc:  # no alembic_version table: not a migrated database
        raise SeedError(f"cannot read alembic_version: {exc}") from exc
    if versions != heads:
        raise SeedError(
            f"the database is at {sorted(versions)}, the code at {sorted(heads)}: "
            "apply the migrations (or use the code of the deployed release) and retry"
        )


def find_users(db: Session, owner_email: str, collaborator_email: str) -> tuple[User, User]:
    users = []
    for role, email in (("owner", owner_email), ("collaborator", collaborator_email)):
        user = user_repo.get_by_email(db, email)
        if user is None:
            raise SeedError(f"{role}: no user with email {email!r}")
        if user.disabled:
            raise SeedError(f"{role}: the user {email!r} is disabled")
        users.append(user)
    owner, collaborator = users
    if owner.id == collaborator.id:
        raise SeedError("owner and collaborator must be two different users")
    return owner, collaborator


def seed(db: Session, owner: User, collaborator: User) -> list[CollProject]:
    """Build the three projects; the caller owns the transaction."""
    names = [spec.name for spec in SPECS]
    existing = db.scalars(
        select(FormProject.name).where(
            FormProject.owner_id == owner.id, FormProject.name.in_(names)
        )
    ).all()
    if existing:
        raise SeedError(
            f"the owner already has {sorted(existing)}: no project created "
            "(the command neither changes nor duplicates existing projects)"
        )
    return [build_project(db, spec, owner=owner, collaborator=collaborator) for spec in SPECS]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tests.synthetic.seed_projects",
        description="Create COLL-MULTI, COLL-SINGLE and COLL-CONFIG in an environment's database.",
    )
    parser.add_argument("--database-url", required=True, help="database of the target environment")
    parser.add_argument("--owner", required=True, help="email of the owner (an existing user)")
    parser.add_argument(
        "--collaborator", required=True, help="email of the second operator (an existing user)"
    )
    args = parser.parse_args(argv)

    engine = create_engine(args.database_url, future=True)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            # The services commit as they go: within the outer transaction
            # those commits only release savepoints, so the three projects are
            # written all together at the end, or not at all.
            session = Session(bind=connection, join_transaction_mode="create_savepoint")
            try:
                check_alembic_head(connection)
                owner, collaborator = find_users(db=session, owner_email=args.owner,
                                                 collaborator_email=args.collaborator)
                projects = seed(session, owner, collaborator)
            except BaseException:
                session.close()
                transaction.rollback()
                raise
            session.close()
            transaction.commit()
    except SeedError as exc:
        print(f"seed_projects: {exc}. Nothing written.", file=sys.stderr)
        return 1
    finally:
        engine.dispose()

    for project in projects:
        variants = ", ".join(
            f"{v.key} ({v.status.value}{', VID ' + v.vid_value if v.vid_value else ''})"
            for v in project.variants.values()
        )
        print(f"{project.code}: project {project.project_id} — variants {variants}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
