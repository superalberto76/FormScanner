# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Data-access layer for FormVariantPage.

Page reads plus blank-page provisioning, deletion and reordering.
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.form_variant_page import FormVariantPage

# ---------------------------------------------------------------------------
# Authoring defaults — A4 portrait at 96 DPI.
# A4 = 210 x 297 mm => 793.7 x 1122.5 px @ 96 DPI, rounded to whole pixels.
# These are explicit, consistent provisioning defaults for every new page.
# ---------------------------------------------------------------------------
A4_96DPI_WIDTH_PX = 794
A4_96DPI_HEIGHT_PX = 1123
A4_96DPI_DPI = 96.0

# Page indices are 1-BASED.
FIRST_PAGE_INDEX = 1


def get(db: Session, page_id: uuid.UUID) -> FormVariantPage | None:
    return db.get(FormVariantPage, page_id)


def list_for_variant(db: Session, variant_id: uuid.UUID) -> list[FormVariantPage]:
    stmt = (
        select(FormVariantPage)
        .where(FormVariantPage.form_variant_id == variant_id)
        .order_by(FormVariantPage.page_index)
    )
    return list(db.scalars(stmt).all())


def page_indices_for_variants(
    db: Session, variant_ids: set[uuid.UUID]
) -> dict[uuid.UUID, set[int]]:
    """{variant_id: its page_index values} for several variants, in ONE query.

    The "expected" half of a submission's missing pages: the batch detail
    needs it for every distinct variant its submissions matched, and one
    ``IN`` query replaces a lookup per variant. A variant with no page is
    ABSENT. Empty input returns {} without touching the database.
    """
    if not variant_ids:
        return {}
    stmt = select(FormVariantPage.form_variant_id, FormVariantPage.page_index).where(
        FormVariantPage.form_variant_id.in_(variant_ids)
    )
    indices: dict[uuid.UUID, set[int]] = {}
    for variant_id, page_index in db.execute(stmt).all():
        indices.setdefault(variant_id, set()).add(page_index)
    return indices


def count_for_variant(db: Session, variant_id: uuid.UUID) -> int:
    """Number of pages a variant has."""
    count = db.scalar(
        select(func.count(FormVariantPage.id)).where(
            FormVariantPage.form_variant_id == variant_id
        )
    )
    return count or 0


def next_page_index(db: Session, variant_id: uuid.UUID) -> int:
    """Next page_index for a variant: max existing index + 1 (1-based).

    With no pages yet this returns FIRST_PAGE_INDEX, matching add_blank_page's
    default for the auto-provisioned first page.
    """
    max_idx = db.scalar(
        select(func.max(FormVariantPage.page_index)).where(
            FormVariantPage.form_variant_id == variant_id
        )
    )
    return (max_idx or FIRST_PAGE_INDEX - 1) + 1


def add_blank_page(
    db: Session,
    variant_id: uuid.UUID,
    page_index: int = FIRST_PAGE_INDEX,
) -> FormVariantPage:
    """Create one blank A4 authoring page for *variant_id* and flush."""
    page = FormVariantPage(
        form_variant_id=variant_id,
        page_index=page_index,
        image_path=None,
        width_px=A4_96DPI_WIDTH_PX,
        height_px=A4_96DPI_HEIGHT_PX,
        dpi=A4_96DPI_DPI,
    )
    db.add(page)
    db.flush()
    return page


def delete(db: Session, page: FormVariantPage) -> None:
    """Delete a single page; ORM cascade (all, delete-orphan) removes its
    PageObjects and their typed configs/children — a SCOPED removal that never
    touches objects on other pages, not even the linked copies of a shared
    object."""
    db.delete(page)
    db.flush()


def set_page_order(
    db: Session, variant_id: uuid.UUID, ordered_ids: list[uuid.UUID]
) -> None:
    """Renumber the given pages to a contiguous 1..n in ``ordered_ids`` order.

    Two-phase to avoid transiently violating the UniqueConstraint
    (form_variant_id, page_index): phase 1 parks every page at a temporary index
    beyond the current max, phase 2 assigns the final 1-based indices. Remains
    correct even if the constraint is ever dropped. ``ordered_ids`` is expected to
    list exactly the variant's current pages (validated by the caller); any id not
    belonging to the variant is ignored.
    """
    pages = {p.id: p for p in list_for_variant(db, variant_id)}
    max_idx = db.scalar(
        select(func.max(FormVariantPage.page_index)).where(
            FormVariantPage.form_variant_id == variant_id
        )
    ) or 0

    # Phase 1 — temporary indices above the current max (collision-free).
    for offset, page_id in enumerate(ordered_ids):
        page = pages.get(page_id)
        if page is not None:
            page.page_index = max_idx + 1 + offset
    db.flush()

    # Phase 2 — final contiguous 1..n in the requested order.
    for i, page_id in enumerate(ordered_ids):
        page = pages.get(page_id)
        if page is not None:
            page.page_index = i + FIRST_PAGE_INDEX
    db.flush()
