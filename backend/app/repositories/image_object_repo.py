# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Data-access layer for ImageObjectConfig."""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.form_variant import FormVariant
from app.models.form_variant_page import FormVariantPage
from app.models.image_object_config import ImageObjectConfig
from app.models.page_object import PageObject


def add(db: Session, config: ImageObjectConfig) -> ImageObjectConfig:
    db.add(config)
    db.flush()
    return config


def list_file_paths_for_variant(db: Session, variant_id: uuid.UUID) -> list[str]:
    """Absolute file paths of every uploaded IMAGE under a variant.

    Used to delete the on-disk files when a variant is deleted (the ORM cascade
    removes the config rows but not the binaries).
    """
    stmt = (
        select(ImageObjectConfig.file_path)
        .join(PageObject, ImageObjectConfig.page_object_id == PageObject.id)
        .join(FormVariantPage, PageObject.form_variant_page_id == FormVariantPage.id)
        .where(FormVariantPage.form_variant_id == variant_id)
        .where(ImageObjectConfig.file_path.is_not(None))
    )
    return [p for p in db.scalars(stmt).all() if p]


def list_file_paths_for_page(db: Session, page_id: uuid.UUID) -> list[str]:
    """Absolute file paths of every uploaded IMAGE on a single page.

    Used to delete the on-disk files when a page is removed (the ORM cascade
    removes the config rows but not the binaries).
    """
    stmt = (
        select(ImageObjectConfig.file_path)
        .join(PageObject, ImageObjectConfig.page_object_id == PageObject.id)
        .where(PageObject.form_variant_page_id == page_id)
        .where(ImageObjectConfig.file_path.is_not(None))
    )
    return [p for p in db.scalars(stmt).all() if p]


def list_file_paths_for_project(db: Session, project_id: uuid.UUID) -> list[str]:
    """Absolute file paths of every uploaded IMAGE under a whole project."""
    stmt = (
        select(ImageObjectConfig.file_path)
        .join(PageObject, ImageObjectConfig.page_object_id == PageObject.id)
        .join(FormVariantPage, PageObject.form_variant_page_id == FormVariantPage.id)
        .join(FormVariant, FormVariantPage.form_variant_id == FormVariant.id)
        .where(FormVariant.form_project_id == project_id)
        .where(ImageObjectConfig.file_path.is_not(None))
    )
    return [p for p in db.scalars(stmt).all() if p]
