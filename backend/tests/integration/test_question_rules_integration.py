# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for QUESTION question_type enforcement.

SINGLE_CHOICE keeps at most one correct option (auto-resolve, not 409);
MULTIPLE_CHOICE is unconstrained. Covers the real service wiring: bulk creation,
per-option create/update, and the config type-switch reconcile. Requires
TEST_DATABASE_URL — see conftest.py (fixtures user/page are shared).
"""
from __future__ import annotations

from sqlalchemy import select

from app.models.enums import PageObjectType, QuestionType
from app.models.page_object import PageObject
from app.repositories import question_object_repo
from app.schemas.question_object import (
    QuestionObjectConfigCreate,
    QuestionObjectConfigUpdate,
    QuestionOptionCreate,
    QuestionOptionUpdate,
    QuestionPageObjectCreate,
)
from app.services import question_object_service as svc

Q = QuestionType


def _question_data(qtype, options, *, text="Q?"):
    return QuestionPageObjectCreate(
        x=60, y=60, width=100, height=100,
        config=QuestionObjectConfigCreate(
            question_type=qtype, question_text=text, options=options,
        ),
    )


def _opt(i, *, correct=False):
    return QuestionOptionCreate(order_index=i, option_text=f"opt{i}", is_correct=correct)


def _config(db, page):
    obj = db.scalars(
        select(PageObject).where(PageObject.form_variant_page_id == page.id)
    ).one()
    assert obj.object_type == PageObjectType.QUESTION
    return obj.question_config


def _options(db, config):
    return question_object_repo.list_options(db, config.id)


def _correct_indices(db, config):
    return [o.order_index for o in _options(db, config) if o.is_correct]


def _make_question(db, page, user, qtype, options):
    svc.create_question(db, page.id, _question_data(qtype, options), user)
    return _config(db, page)


# --------------------------------------------------------------------------
# Bulk creation
# --------------------------------------------------------------------------
def test_create_single_choice_two_correct_keeps_first(db, page, user):
    cfg = _make_question(db, page, user, Q.SINGLE_CHOICE, [
        _opt(0, correct=True), _opt(1, correct=True), _opt(2),
    ])
    assert _correct_indices(db, cfg) == [0]


def test_create_multiple_choice_keeps_all_correct(db, page, user):
    cfg = _make_question(db, page, user, Q.MULTIPLE_CHOICE, [
        _opt(0, correct=True), _opt(1, correct=True), _opt(2),
    ])
    assert _correct_indices(db, cfg) == [0, 1]


# --------------------------------------------------------------------------
# Per-option create
# --------------------------------------------------------------------------
def test_create_option_correct_clears_previous_single_choice(db, page, user):
    cfg = _make_question(db, page, user, Q.SINGLE_CHOICE, [_opt(0, correct=True)])
    obj_id = cfg.page_object_id
    svc.create_option(db, obj_id, _opt(1, correct=True), user)
    # the newly added option wins; the old correct one is cleared
    assert _correct_indices(db, cfg) == [1]


def test_create_option_correct_multiple_choice_keeps_both(db, page, user):
    cfg = _make_question(db, page, user, Q.MULTIPLE_CHOICE, [_opt(0, correct=True)])
    svc.create_option(db, cfg.page_object_id, _opt(1, correct=True), user)
    assert _correct_indices(db, cfg) == [0, 1]


# --------------------------------------------------------------------------
# Per-option update
# --------------------------------------------------------------------------
def test_update_option_set_correct_clears_others_single_choice(db, page, user):
    cfg = _make_question(db, page, user, Q.SINGLE_CHOICE, [
        _opt(0, correct=True), _opt(1),
    ])
    second = next(o for o in _options(db, cfg) if o.order_index == 1)
    svc.update_option(db, second.id, QuestionOptionUpdate(is_correct=True), user)
    assert _correct_indices(db, cfg) == [1]


def test_update_option_set_correct_multiple_choice_keeps_others(db, page, user):
    cfg = _make_question(db, page, user, Q.MULTIPLE_CHOICE, [
        _opt(0, correct=True), _opt(1),
    ])
    second = next(o for o in _options(db, cfg) if o.order_index == 1)
    svc.update_option(db, second.id, QuestionOptionUpdate(is_correct=True), user)
    assert _correct_indices(db, cfg) == [0, 1]


# --------------------------------------------------------------------------
# Config type switch reconcile
# --------------------------------------------------------------------------
def test_switch_to_single_choice_reconciles_existing_options(db, page, user):
    cfg = _make_question(db, page, user, Q.MULTIPLE_CHOICE, [
        _opt(0, correct=True), _opt(1, correct=True), _opt(2, correct=True),
    ])
    svc.update_question_config(
        db, cfg.page_object_id,
        QuestionObjectConfigUpdate(question_type=Q.SINGLE_CHOICE), user,
    )
    assert _correct_indices(db, cfg) == [0]


def test_switch_to_multiple_choice_leaves_single_correct(db, page, user):
    cfg = _make_question(db, page, user, Q.SINGLE_CHOICE, [
        _opt(0, correct=True), _opt(1),
    ])
    svc.update_question_config(
        db, cfg.page_object_id,
        QuestionObjectConfigUpdate(question_type=Q.MULTIPLE_CHOICE), user,
    )
    assert _correct_indices(db, cfg) == [0]
