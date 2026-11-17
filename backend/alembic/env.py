# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Alembic environment configuration."""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool
from alembic import context

# ---------------------------------------------------------------------------
# Ensure the backend package root (the directory that contains 'app/') is on
# sys.path.  'prepend_sys_path = .' in alembic.ini handles the common case,
# but this block is a belt-and-suspenders guard for environments where Alembic
# is invoked from a directory other than backend/.
# ---------------------------------------------------------------------------
_backend_dir = Path(__file__).resolve().parent.parent  # .../backend/
if str(_backend_dir) not in sys.path:
    sys.path.insert(0, str(_backend_dir))

# ---------------------------------------------------------------------------
# Import ORM metadata AFTER sys.path is set.
# Importing the package executes models/__init__.py, which imports every
# model module, ensuring Base.metadata contains all table definitions.
# ---------------------------------------------------------------------------
from app.models import Base  # noqa: E402

# Alembic Config — gives access to the values in alembic.ini.
config = context.config

# ---------------------------------------------------------------------------
# DATABASE_URL env var overrides the sqlalchemy.url value in alembic.ini.
# This is the mechanism used in Docker / CI where credentials live in the
# environment, not in the checked-in .ini file.
# ---------------------------------------------------------------------------
_database_url = os.environ.get("DATABASE_URL")
if _database_url:
    config.set_main_option("sqlalchemy.url", _database_url)

# Apply logging configuration from alembic.ini (if a file is present).
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The metadata object Alembic compares against when running --autogenerate.
target_metadata = Base.metadata


# ---------------------------------------------------------------------------
# Offline mode — emit DDL SQL to stdout/file without a live DB connection.
# Useful for generating SQL scripts to review before applying.
# ---------------------------------------------------------------------------
def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online mode — connect to the database and apply migrations directly.
# NullPool is used so Alembic does not hold idle connections between runs.
# ---------------------------------------------------------------------------
def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
