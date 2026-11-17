# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Data-access layer for TextObjectConfig."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.text_object_config import TextObjectConfig


def add(db: Session, config: TextObjectConfig) -> TextObjectConfig:
    db.add(config)
    db.flush()
    return config
