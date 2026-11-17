# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bootstrap script executed before uvicorn starts.

Run automatically via:
  python -m app.bootstrap

Steps performed:
1. Ensure required storage directories exist.
2. Ensure the initial admin user exists in the database (idempotent).

Exit behaviour on DB errors:
- OperationalError (DB not reachable): always warns and continues.
  Uvicorn will start and the health-check will surface the problem.
- ProgrammingError (schema missing, i.e. migrations not run):
  warns and exits with code 1 when APP_ENV=production;
  in all other environments it warns and continues.
"""
from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import exc as sa_exc

from app.core.config import settings
from app.core.security import hash_password
from app.models.enums import UserRole


def ensure_dirs() -> None:
    for p in [settings.storage_path, settings.original_uploads_dir, settings.rendered_pages_dir]:
        Path(p).mkdir(parents=True, exist_ok=True)


def ensure_initial_admin_user() -> None:
    """Create or repair the initial admin user defined by env vars.

    Idempotency rules:
    - No user with INITIAL_ADMIN_EMAIL  → create with role=ADMIN, disabled=false.
    - User exists, role != ADMIN        → set role=ADMIN.
    - User exists, disabled=true        → set disabled=false.
    - FORCE_RESET_ADMIN_PASSWORD=true   → update password hash.
    - All other fields are never touched.
    Plaintext passwords are never printed or logged.
    """
    # Deferred imports keep startup fast when only ensure_dirs() is needed.
    from app.db.session import SessionLocal
    from app.repositories import user_repo

    email = settings.initial_admin_email
    force_reset = settings.force_reset_admin_password

    try:
        with SessionLocal() as db:
            user = user_repo.get_by_email(db, email)

            if user is None:
                user_repo.create_admin_user(
                    db,
                    email=email,
                    password_hash=hash_password(settings.initial_admin_password),
                )
                db.commit()
                print(f"[bootstrap] Admin user created: {email}")
                return

            # User already exists — apply any necessary repairs.
            changed = False

            if user.role != UserRole.ADMIN:
                print(f"[bootstrap] Promoting existing user to ADMIN: {email}")
                user.role = UserRole.ADMIN
                changed = True

            if user.disabled:
                print(f"[bootstrap] Re-enabling disabled admin user: {email}")
                user.disabled = False
                changed = True

            if force_reset:
                print(f"[bootstrap] Resetting password for admin user: {email}")
                user.password_hash = hash_password(settings.initial_admin_password)
                changed = True

            if changed:
                db.commit()
                print(f"[bootstrap] Admin user updated: {email}")
            else:
                print(f"[bootstrap] Admin user already exists, no changes needed: {email}")

    except sa_exc.OperationalError as exc:
        # DB not reachable yet (e.g. container still starting).
        print(f"[bootstrap] WARNING: DB not reachable — {exc.orig}")
        print("[bootstrap] Admin bootstrap skipped. The app will start but may not be usable until the DB is available.")

    except sa_exc.ProgrammingError as exc:
        # Schema missing: alembic upgrade head has not been run.
        print(f"[bootstrap] WARNING: DB schema not initialised — {exc.orig}")
        print("[bootstrap] Run 'alembic upgrade head' before starting the application.")
        if settings.app_env == "production":
            sys.exit(1)


if __name__ == '__main__':
    ensure_dirs()
    ensure_initial_admin_user()
