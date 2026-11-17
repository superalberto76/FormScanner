# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Data-access layer for FreeTextAreaConfig."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.free_text_area_config import FreeTextAreaConfig


def add(db: Session, config: FreeTextAreaConfig) -> FreeTextAreaConfig:
    db.add(config)
    db.flush()
    return config
