# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""QUESTION PageObject service.

Covers the QuestionObjectConfig plus QuestionOption children. Option
``order_index`` is unique per question (enforced here and by a DB unique
constraint).
"""
from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.enums import PageObjectType, QuestionType
from app.models.page_object import PageObject
from app.models.question_object_config import QuestionObjectConfig
from app.models.question_option import QuestionOption
from app.models.user import User
from app.repositories import page_object_repo, question_object_repo
from app.schemas.question_object import (
    QuestionObjectConfigUpdate,
    QuestionOptionCreate,
    QuestionOptionRead,
    QuestionOptionUpdate,
    QuestionPageObjectCreate,
)
from app.services import audit_service, page_object_service


def _reconcile_single_choice(db: Session, config_id: uuid.UUID) -> None:
    """Keep at most one correct option for a SINGLE_CHOICE question.

    Deterministic: the FIRST correct option by order_index wins, the rest are
    cleared. Used by bulk create and by config updates that switch the type to
    SINGLE_CHOICE — the authoritative counterpart to the FE radio UX. No-op when
    at most one option is already correct.
    """
    seen = False
    for option in question_object_repo.list_options(db, config_id):
        if not option.is_correct:
            continue
        if seen:
            option.is_correct = False
        else:
            seen = True
    db.flush()


# ---------------------------------------------------------------------------
# Create QUESTION object
# ---------------------------------------------------------------------------


def create_question(
    db: Session, page_id: uuid.UUID, data: QuestionPageObjectCreate, user: User
):
    page, _ = page_object_service.load_page_authorized(
        db, page_id, user, require_draft=True
    )
    # Reserved bands: nothing is placed in them.
    page_object_service.assert_outside_bands(data, page)

    # Validate provided option order_index uniqueness up-front.
    indices = [o.order_index for o in data.config.options]
    if len(indices) != len(set(indices)):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Duplicate option order_index in request.",
        )

    obj = PageObject(
        form_variant_page_id=page.id,
        object_type=PageObjectType.QUESTION,
        x=data.x, y=data.y, width=data.width, height=data.height,
        z_index=data.z_index, label=data.label, description=data.description,
        visible=data.visible, locked=data.locked,
        # "Show on all pages": a fresh linked group, this object its
        # template. Existing pages aren't copied to; new pages get a linked copy.
        shared_group_id=uuid.uuid4() if data.shared_across_pages else None,
        shared_across_pages=data.shared_across_pages,
    )
    page_object_repo.add(db, obj)

    cfg = data.config
    config = question_object_repo.add_config(
        db,
        QuestionObjectConfig(
            page_object_id=obj.id,
            question_type=cfg.question_type,
            question_text=cfg.question_text,
            shuffle_answers=cfg.shuffle_answers,
        ),
    )
    for opt in cfg.options:
        question_object_repo.add_option(
            db,
            QuestionOption(
                question_object_config_id=config.id,
                order_index=opt.order_index,
                option_text=opt.option_text,
                is_correct=opt.is_correct,
                score=opt.score,
            ),
        )
    # SINGLE_CHOICE accepts at most one correct option: reconcile a batch that
    # marks several (keep the first), instead of rejecting.
    if cfg.question_type == QuestionType.SINGLE_CHOICE:
        _reconcile_single_choice(db, config.id)

    audit_service.log_event(
        db, actor_user_id=user.id, action=audit_service.PAGE_OBJECT_CREATED,
        entity_type="PageObject", entity_id=obj.id,
        metadata={"object_type": "QUESTION", "options": len(cfg.options)},
    )
    db.commit()
    db.refresh(obj)
    return page_object_service.build_typed_read(obj)


# ---------------------------------------------------------------------------
# Update QUESTION config
# ---------------------------------------------------------------------------


def update_question_config(
    db: Session, object_id: uuid.UUID, data: QuestionObjectConfigUpdate, user: User
):
    obj, _ = page_object_service.load_object_authorized(
        db, object_id, user, require_draft=True
    )
    page_object_service.assert_object_type(obj, PageObjectType.QUESTION)

    config = obj.question_config
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Question config not found"
        )

    changes = data.model_dump(exclude_unset=True)
    if changes:
        for field, value in changes.items():
            setattr(config, field, value)
        db.flush()
        # Switching to (or staying) SINGLE_CHOICE: reconcile any pre-existing
        # multi-correct options to a single correct one, so the API
        # never leaves an inconsistent config even if called directly.
        if config.question_type == QuestionType.SINGLE_CHOICE:
            _reconcile_single_choice(db, config.id)
        audit_service.log_event(
            db, actor_user_id=user.id,
            action=audit_service.PAGE_OBJECT_CONFIG_UPDATED,
            entity_type="QuestionObjectConfig", entity_id=config.id,
            metadata={k: (v.value if hasattr(v, "value") else v) for k, v in changes.items()},
        )
        db.commit()
    db.refresh(obj)
    return page_object_service.build_typed_read(obj)


# ---------------------------------------------------------------------------
# Question options
# ---------------------------------------------------------------------------


def _load_config_for_object(
    db: Session, object_id: uuid.UUID, user: User, *, require_draft: bool
) -> QuestionObjectConfig:
    obj, _ = page_object_service.load_object_authorized(
        db, object_id, user, require_draft=require_draft
    )
    page_object_service.assert_object_type(obj, PageObjectType.QUESTION)
    if obj.question_config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Question config not found"
        )
    return obj.question_config


def _load_option_authorized(
    db: Session, option_id: uuid.UUID, user: User, *, require_draft: bool
) -> QuestionOption:
    option = question_object_repo.get_option(db, option_id)
    if option is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Question option not found"
        )
    config = db.get(QuestionObjectConfig, option.question_object_config_id)
    # Authorize (and optionally enforce DRAFT) via the owning page object.
    page_object_service.load_object_authorized(
        db, config.page_object_id, user, require_draft=require_draft
    )
    return option


def list_options(
    db: Session, object_id: uuid.UUID, user: User
) -> list[QuestionOptionRead]:
    config = _load_config_for_object(db, object_id, user, require_draft=False)
    options = question_object_repo.list_options(db, config.id)
    return [QuestionOptionRead.model_validate(o) for o in options]


def create_option(
    db: Session, object_id: uuid.UUID, data: QuestionOptionCreate, user: User
) -> QuestionOptionRead:
    config = _load_config_for_object(db, object_id, user, require_draft=True)

    if question_object_repo.order_index_exists(db, config.id, data.order_index):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"order_index {data.order_index} already exists for this question.",
        )

    option = question_object_repo.add_option(
        db,
        QuestionOption(
            question_object_config_id=config.id,
            order_index=data.order_index,
            option_text=data.option_text,
            is_correct=data.is_correct,
            score=data.score,
        ),
    )
    # SINGLE_CHOICE: marking this option correct clears any previous correct
    # one (auto-resolve, not 409).
    if option.is_correct and config.question_type == QuestionType.SINGLE_CHOICE:
        question_object_repo.clear_correct_options(
            db, config.id, exclude_option_id=option.id
        )
    audit_service.log_event(
        db, actor_user_id=user.id, action=audit_service.QUESTION_OPTION_CREATED,
        entity_type="QuestionOption", entity_id=option.id,
    )
    db.commit()
    db.refresh(option)
    return QuestionOptionRead.model_validate(option)


def update_option(
    db: Session, option_id: uuid.UUID, data: QuestionOptionUpdate, user: User
) -> QuestionOptionRead:
    option = _load_option_authorized(db, option_id, user, require_draft=True)

    changes = data.model_dump(exclude_unset=True)
    if changes:
        new_index = changes.get("order_index")
        if new_index is not None and question_object_repo.order_index_exists(
            db, option.question_object_config_id, new_index,
            exclude_option_id=option.id,
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"order_index {new_index} already exists for this question.",
            )

        for field, value in changes.items():
            setattr(option, field, value)
        db.flush()
        # SINGLE_CHOICE: setting this option correct clears the others
        # (auto-resolve, not 409).
        if changes.get("is_correct") is True:
            config = db.get(QuestionObjectConfig, option.question_object_config_id)
            if config.question_type == QuestionType.SINGLE_CHOICE:
                question_object_repo.clear_correct_options(
                    db, config.id, exclude_option_id=option.id
                )
                db.flush()
        audit_service.log_event(
            db, actor_user_id=user.id, action=audit_service.QUESTION_OPTION_UPDATED,
            entity_type="QuestionOption", entity_id=option.id, metadata=changes,
        )
        db.commit()
    db.refresh(option)
    return QuestionOptionRead.model_validate(option)


def delete_option(db: Session, option_id: uuid.UUID, user: User) -> None:
    option = _load_option_authorized(db, option_id, user, require_draft=True)
    option_id_val = option.id
    question_object_repo.delete_option(db, option)
    audit_service.log_event(
        db, actor_user_id=user.id, action=audit_service.QUESTION_OPTION_DELETED,
        entity_type="QuestionOption", entity_id=option_id_val,
    )
    db.commit()
