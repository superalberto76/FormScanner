# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Data-access layer for QuestionObjectConfig and QuestionOption."""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.question_object_config import QuestionObjectConfig
from app.models.question_option import QuestionOption


# --- config ---------------------------------------------------------------


def add_config(db: Session, config: QuestionObjectConfig) -> QuestionObjectConfig:
    db.add(config)
    db.flush()
    return config


# --- options ---------------------------------------------------------------


def get_option(db: Session, option_id: uuid.UUID) -> QuestionOption | None:
    return db.get(QuestionOption, option_id)


def add_option(db: Session, option: QuestionOption) -> QuestionOption:
    db.add(option)
    db.flush()
    return option


def list_options(db: Session, config_id: uuid.UUID) -> list[QuestionOption]:
    stmt = (
        select(QuestionOption)
        .where(QuestionOption.question_object_config_id == config_id)
        .order_by(QuestionOption.order_index)
    )
    return list(db.scalars(stmt).all())


def order_index_exists(
    db: Session,
    config_id: uuid.UUID,
    order_index: int,
    *,
    exclude_option_id: uuid.UUID | None = None,
) -> bool:
    stmt = select(QuestionOption.id).where(
        QuestionOption.question_object_config_id == config_id,
        QuestionOption.order_index == order_index,
    )
    if exclude_option_id is not None:
        stmt = stmt.where(QuestionOption.id != exclude_option_id)
    return db.scalars(stmt).first() is not None


def clear_correct_options(
    db: Session,
    config_id: uuid.UUID,
    *,
    exclude_option_id: uuid.UUID | None = None,
) -> None:
    """Set is_correct=False on every correct option of a config, except one.

    Auto-resolves the SINGLE_CHOICE invariant (at most one correct option): the
    option just marked correct is kept (via ``exclude_option_id``), all its
    correct siblings cleared.
    """
    stmt = select(QuestionOption).where(
        QuestionOption.question_object_config_id == config_id,
        QuestionOption.is_correct.is_(True),
    )
    if exclude_option_id is not None:
        stmt = stmt.where(QuestionOption.id != exclude_option_id)
    for option in db.scalars(stmt):
        option.is_correct = False
    db.flush()


def delete_option(db: Session, option: QuestionOption) -> None:
    db.delete(option)
    db.flush()
