# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pydantic schemas for QUESTION PageObjects."""
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import PageObjectType, QuestionType
from app.schemas.page_object import PageObjectBaseRead, PageObjectCreateBase


# ---------------------------------------------------------------------------
# QuestionOption
# ---------------------------------------------------------------------------


class QuestionOptionBase(BaseModel):
    order_index: int = Field(ge=0)
    option_text: str
    is_correct: bool = False
    # May be negative (penalty for a distractor).
    score: float | None = None


class QuestionOptionCreate(QuestionOptionBase):
    option_text: str = Field(min_length=1)


class QuestionOptionUpdate(BaseModel):
    order_index: int | None = Field(default=None, ge=0)
    option_text: str | None = Field(default=None, min_length=1)
    is_correct: bool | None = None
    score: float | None = None


class QuestionOptionRead(QuestionOptionBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    question_object_config_id: uuid.UUID
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# QuestionObjectConfig
# ---------------------------------------------------------------------------


class QuestionObjectConfigBase(BaseModel):
    question_type: QuestionType
    question_text: str
    shuffle_answers: bool = False


class QuestionObjectConfigCreate(QuestionObjectConfigBase):
    question_text: str = Field(min_length=1)
    options: list[QuestionOptionCreate] = Field(default_factory=list)


class QuestionObjectConfigUpdate(BaseModel):
    # Options are managed via the dedicated /question-options endpoints.
    question_type: QuestionType | None = None
    question_text: str | None = Field(default=None, min_length=1)
    shuffle_answers: bool | None = None


class QuestionObjectConfigRead(QuestionObjectConfigBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    page_object_id: uuid.UUID
    options: list[QuestionOptionRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# QUESTION typed create / read
# ---------------------------------------------------------------------------


class QuestionPageObjectCreate(PageObjectCreateBase):
    config: QuestionObjectConfigCreate


class QuestionPageObjectRead(PageObjectBaseRead):
    object_type: Literal[PageObjectType.QUESTION] = PageObjectType.QUESTION
    question_config: QuestionObjectConfigRead
