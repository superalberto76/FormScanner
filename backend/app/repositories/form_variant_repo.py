# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Data-access layer for FormVariant."""
from __future__ import annotations

import uuid

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.enums import FormVariantStatus
from app.models.form_variant import FormVariant
from app.models.form_variant_page import FormVariantPage


def get(db: Session, variant_id: uuid.UUID) -> FormVariant | None:
    return db.get(FormVariant, variant_id)


def add(db: Session, variant: FormVariant) -> FormVariant:
    db.add(variant)
    db.flush()  # populate variant.id
    return variant


def list_for_project(db: Session, project_id: uuid.UUID) -> list[FormVariant]:
    """All variants of a project, ordered by version_number."""
    stmt = (
        select(FormVariant)
        .where(FormVariant.form_project_id == project_id)
        .order_by(FormVariant.version_number)
    )
    return list(db.scalars(stmt).all())


def get_next_version_number(db: Session, project_id: uuid.UUID) -> int:
    """Next version_number within a project (1-based, gap-free per project)."""
    max_num = db.scalar(
        select(func.max(FormVariant.version_number)).where(
            FormVariant.form_project_id == project_id
        )
    )
    return (max_num or 0) + 1


def page_counts(
    db: Session, variant_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Map of variant_id -> number of pages, for the given variant ids."""
    if not variant_ids:
        return {}
    stmt = (
        select(FormVariantPage.form_variant_id, func.count(FormVariantPage.id))
        .where(FormVariantPage.form_variant_id.in_(variant_ids))
        .group_by(FormVariantPage.form_variant_id)
    )
    return {vid: count for vid, count in db.execute(stmt).all()}


def count_pages(db: Session, variant_id: uuid.UUID) -> int:
    count = db.scalar(
        select(func.count(FormVariantPage.id)).where(
            FormVariantPage.form_variant_id == variant_id
        )
    )
    return count or 0


def count_published_variants(db: Session, project_id: uuid.UUID) -> int:
    """Number of PUBLISHED variants of *project_id*.

    Backs the DRAFT→ACTIVE transition gate: a project may only go ACTIVE once it
    has at least one PUBLISHED variant to print/scan.
    """
    count = db.scalar(
        select(func.count(FormVariant.id)).where(
            FormVariant.form_project_id == project_id,
            FormVariant.status == FormVariantStatus.PUBLISHED,
        )
    )
    return count or 0


def list_published_variant_ids(
    db: Session, project_id: uuid.UUID
) -> list[uuid.UUID]:
    """Ids of every PUBLISHED variant of *project_id*.

    Backs the scan-readiness check: the caller needs the actual ids
    (to look for a VARIANT_IDENTIFIER binding among them), not just the count
    that ``count_published_variants`` returns.
    """
    stmt = select(FormVariant.id).where(
        FormVariant.form_project_id == project_id,
        FormVariant.status == FormVariantStatus.PUBLISHED,
    )
    return list(db.scalars(stmt).all())


def archive_published_variants(db: Session, project_id: uuid.UUID) -> int:
    """Bulk-archive a project's PUBLISHED variants (the archive cascade).

    Sets ``status = ARCHIVED`` for every PUBLISHED variant of *project_id* and
    returns the number of rows changed. DRAFT variants are left as DRAFT and
    already-ARCHIVED ones are untouched (the WHERE filters on PUBLISHED only).

    ``synchronize_session=False``: the caller commits right after, which expires
    the session, so any in-memory variant rows reload their fresh status on next
    access — no stale ORM state survives the cascade.
    """
    result = db.execute(
        update(FormVariant)
        .where(
            FormVariant.form_project_id == project_id,
            FormVariant.status == FormVariantStatus.PUBLISHED,
        )
        .values(status=FormVariantStatus.ARCHIVED)
        .execution_options(synchronize_session=False)
    )
    db.flush()
    return result.rowcount


def delete(db: Session, variant: FormVariant) -> None:
    """Delete a variant; ORM cascade removes its pages, PageObjects and their
    typed configs (text/code/free_text/question+options/matrix+rows/cols/cells).
    """
    db.delete(variant)
    db.flush()
