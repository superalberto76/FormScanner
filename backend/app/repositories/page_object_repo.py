# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Data-access layer for PageObject."""
from __future__ import annotations

import uuid
from collections.abc import Collection

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.models.enums import PageObjectType
from app.models.form_variant_page import FormVariantPage
from app.models.matrix_object_config import MatrixObjectConfig
from app.models.page_object import PageObject
from app.models.question_object_config import QuestionObjectConfig

# The full set of typed-config eager-load options, reused by the single-object
# and per-page "with configs" loaders so the deep-copy graph is fetched in one go.
# ``role_binding`` is loaded too so the enriched page list can expose
# each object's ``role`` without an extra per-object query (no N+1).
_CONFIG_LOAD_OPTIONS = (
    selectinload(PageObject.text_config),
    selectinload(PageObject.code_config),
    selectinload(PageObject.free_text_config),
    selectinload(PageObject.image_config),
    selectinload(PageObject.question_config).selectinload(
        QuestionObjectConfig.options
    ),
    selectinload(PageObject.matrix_config).selectinload(MatrixObjectConfig.rows),
    selectinload(PageObject.matrix_config).selectinload(MatrixObjectConfig.cols),
    selectinload(PageObject.matrix_config).selectinload(MatrixObjectConfig.cells),
    selectinload(PageObject.role_binding),
)


def get(db: Session, object_id: uuid.UUID) -> PageObject | None:
    return db.get(PageObject, object_id)


def get_with_configs(db: Session, object_id: uuid.UUID) -> PageObject | None:
    """Fetch a single object with every typed config (+ children) eagerly loaded.

    Used by the single-object deep-copy helper (shared-group lazy replication),
    which needs the full object graph in one query.
    """
    stmt = (
        select(PageObject)
        .where(PageObject.id == object_id)
        .options(*_CONFIG_LOAD_OPTIONS)
    )
    return db.scalars(stmt).first()


def add(db: Session, obj: PageObject) -> PageObject:
    db.add(obj)
    db.flush()  # populate obj.id
    return obj


def list_for_page(db: Session, page_id: uuid.UUID) -> list[PageObject]:
    """All objects on a page, ordered by z_index then created_at."""
    stmt = (
        select(PageObject)
        .where(PageObject.form_variant_page_id == page_id)
        .order_by(PageObject.z_index, PageObject.created_at)
    )
    return list(db.scalars(stmt).all())


def list_for_page_with_configs(db: Session, page_id: uuid.UUID) -> list[PageObject]:
    """All objects on a page with every typed config (+ children) eagerly loaded.

    Used by the clone path, which deep-copies the full object graph: eager
    loading the one-to-one configs and their children (question options, matrix
    rows/cols/cells) up front avoids the N+1 that per-object lazy access causes.
    Ordering matches ``list_for_page`` (z_index, then created_at).
    """
    stmt = (
        select(PageObject)
        .where(PageObject.form_variant_page_id == page_id)
        .order_by(PageObject.z_index, PageObject.created_at)
        .options(*_CONFIG_LOAD_OPTIONS)
    )
    return list(db.scalars(stmt).all())


def list_by_shared_group(
    db: Session, shared_group_id: uuid.UUID
) -> list[PageObject]:
    """Every object belonging to a shared group (linked AND detached members).

    Group ids are fresh per variant (the clone path remaps them), so this is
    variant-local in practice. Callers filter by ``shared_across_pages`` when
    they only want the linked members.
    """
    stmt = select(PageObject).where(PageObject.shared_group_id == shared_group_id)
    return list(db.scalars(stmt).all())


def linked_group_templates_for_variant(
    db: Session,
    variant_id: uuid.UUID,
    *,
    exclude_page_id: uuid.UUID | None = None,
) -> dict[uuid.UUID, uuid.UUID]:
    """Map each linked shared group of a variant to its template object id.

    The template is chosen deterministically as the linked member sitting on the
    lowest page_index (ties broken by created_at). Used by the lazy replication
    on add-page to know what to copy onto a freshly created page.
    """
    stmt = (
        select(
            PageObject.shared_group_id,
            PageObject.id,
            FormVariantPage.page_index,
        )
        .join(
            FormVariantPage,
            PageObject.form_variant_page_id == FormVariantPage.id,
        )
        .where(FormVariantPage.form_variant_id == variant_id)
        .where(PageObject.shared_across_pages.is_(True))
        .where(PageObject.shared_group_id.is_not(None))
        .order_by(
            PageObject.shared_group_id,
            FormVariantPage.page_index,
            PageObject.created_at,
        )
    )
    if exclude_page_id is not None:
        stmt = stmt.where(PageObject.form_variant_page_id != exclude_page_id)

    templates: dict[uuid.UUID, uuid.UUID] = {}
    for group_id, object_id, _page_index in db.execute(stmt).all():
        # First row per group_id is the lowest page_index (see order_by).
        templates.setdefault(group_id, object_id)
    return templates


def delete(db: Session, obj: PageObject) -> None:
    # Cascades to the typed config (+ its children) via ORM delete-orphan.
    db.delete(obj)
    db.flush()


def list_for_template_pages_with_role(
    db: Session, page_ids: Collection[uuid.UUID]
) -> list[PageObject]:
    """Every object authored on *page_ids*, with its role binding, in ONE
    query.

    The role is joined because deciding whether an object is extractable needs
    it (a role can veto or reroute an extractor), and so are the bindings of
    its linked group: a copy of a shared object carries the group's role
    (``PageObject.effective_role``). Unordered: callers group by
    page. Empty input returns [] without touching the database.
    """
    if not page_ids:
        return []
    stmt = (
        select(PageObject)
        .where(PageObject.form_variant_page_id.in_(page_ids))
        .options(
            joinedload(PageObject.role_binding),
            selectinload(PageObject.group_role_bindings),
        )
    )
    return list(db.scalars(stmt).all())


_EXPORTABLE_TYPES = (
    PageObjectType.QUESTION,
    PageObjectType.MATRIX,
    PageObjectType.FREE_TEXT_AREA,
    PageObjectType.CODE,
)


def list_exportable_for_variants(
    db: Session, variant_ids: Collection[uuid.UUID]
) -> list[PageObject]:
    """Every QUESTION, MATRIX, FREE_TEXT_AREA and CODE of these variants, with
    what the Detail export reads, in ONE statement plus
    one per eager-loaded relation, however many variants and objects.

    Loaded: the template page (``page_index`` and ``form_variant_id``), the
    question options, the matrix rows, columns and cells, the free-text
    config (its placeholder describes the column in the legend), the role
    bindings (own and of the linked group, for ``effective_role``). Not the
    code config: a code's value comes from the result, its legend from the
    label. Which of them get columns, and in which order, is
    ``export_detail``'s business.
    """
    if not variant_ids:
        return []
    stmt = (
        select(PageObject)
        .join(
            FormVariantPage,
            PageObject.form_variant_page_id == FormVariantPage.id,
        )
        .where(FormVariantPage.form_variant_id.in_(variant_ids))
        .where(PageObject.object_type.in_(_EXPORTABLE_TYPES))
        .order_by(FormVariantPage.page_index, PageObject.created_at, PageObject.id)
        .options(
            joinedload(PageObject.form_variant_page),
            selectinload(PageObject.question_config).selectinload(
                QuestionObjectConfig.options
            ),
            selectinload(PageObject.matrix_config).selectinload(MatrixObjectConfig.rows),
            selectinload(PageObject.matrix_config).selectinload(MatrixObjectConfig.cols),
            selectinload(PageObject.matrix_config).selectinload(MatrixObjectConfig.cells),
            selectinload(PageObject.free_text_config),
            selectinload(PageObject.role_binding),
            selectinload(PageObject.group_role_bindings),
        )
    )
    return list(db.scalars(stmt).all())


def list_scorable_for_variant(
    db: Session, variant_id: uuid.UUID
) -> list[PageObject]:
    """Every QUESTION and MATRIX of a variant, in physical page order, with
    what the maximum score reads.

    Options, cells and the role bindings (own and of the linked group, for
    ``scoring_rules.is_scored_matrix``) are eager-loaded: a constant number of
    queries however many objects. Ordered by page_index, then created_at, so
    the first linked copy of a group is the one on the lowest page, as in
    :func:`linked_group_templates_for_variant`.
    """
    stmt = (
        select(PageObject)
        .join(
            FormVariantPage,
            PageObject.form_variant_page_id == FormVariantPage.id,
        )
        .where(FormVariantPage.form_variant_id == variant_id)
        .where(
            PageObject.object_type.in_(
                (PageObjectType.QUESTION, PageObjectType.MATRIX)
            )
        )
        .order_by(FormVariantPage.page_index, PageObject.created_at, PageObject.id)
        .options(
            selectinload(PageObject.question_config).selectinload(
                QuestionObjectConfig.options
            ),
            selectinload(PageObject.matrix_config).selectinload(
                MatrixObjectConfig.cells
            ),
            selectinload(PageObject.role_binding),
            selectinload(PageObject.group_role_bindings),
        )
    )
    return list(db.scalars(stmt).all())


def list_for_variant_with_page(
    db: Session, variant_id: uuid.UUID
) -> list[PageObject]:
    """Every object of a variant, hidden ones included, with its page loaded,
    in page order then z-order (the reserved-band check at publish)."""
    stmt = (
        select(PageObject)
        .join(
            FormVariantPage,
            PageObject.form_variant_page_id == FormVariantPage.id,
        )
        .where(FormVariantPage.form_variant_id == variant_id)
        .order_by(
            FormVariantPage.page_index,
            PageObject.z_index,
            PageObject.created_at,
            PageObject.id,
        )
        .options(joinedload(PageObject.form_variant_page))
    )
    return list(db.scalars(stmt).all())
