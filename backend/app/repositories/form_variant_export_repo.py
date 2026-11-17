# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Data-access for the PDF export — loads a FormVariant with its full object
graph in a bounded number of queries (no N+1).

One ``selectinload`` is emitted per relationship level, so the whole variant
(pages → objects → each typed config → each child collection) loads in a small,
constant number of SELECTs regardless of how many objects exist.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.form_variant import FormVariant
from app.models.form_variant_page import FormVariantPage
from app.models.matrix_object_config import MatrixObjectConfig
from app.models.page_object import PageObject
from app.models.question_object_config import QuestionObjectConfig


def get_variant_for_export(
    db: Session, variant_id: uuid.UUID
) -> FormVariant | None:
    objects = selectinload(FormVariant.pages).selectinload(
        FormVariantPage.page_objects
    )
    stmt = (
        select(FormVariant)
        .where(FormVariant.id == variant_id)
        .options(
            objects.selectinload(PageObject.text_config),
            objects.selectinload(PageObject.code_config),
            objects.selectinload(PageObject.free_text_config),
            objects.selectinload(PageObject.image_config),
            # The renderer reads ``PageObject.effective_role`` to tag the CODE
            # payload on every linked copy; eager-loading keeps that a
            # constant-query lookup.
            objects.selectinload(PageObject.role_binding),
            objects.selectinload(PageObject.group_role_bindings),
            objects.selectinload(PageObject.question_config).selectinload(
                QuestionObjectConfig.options
            ),
            objects.selectinload(PageObject.matrix_config).selectinload(
                MatrixObjectConfig.rows
            ),
            objects.selectinload(PageObject.matrix_config).selectinload(
                MatrixObjectConfig.cols
            ),
            objects.selectinload(PageObject.matrix_config).selectinload(
                MatrixObjectConfig.cells
            ),
        )
    )
    return db.scalars(stmt).first()
