# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for project/variant deletion.

Covers the four behaviours of the delete gates against real Postgres (the
models use postgresql.UUID + native enums, so SQLite is not an option):

  1. delete a DRAFT variant  -> OK, and the whole object subtree cascades away
  2. delete a non-DRAFT variant -> 409, nothing removed
  3. delete a project whose variants are ALL DRAFT -> OK, full cascade
  4. delete a project with at least one non-DRAFT variant -> 409, nothing removed

Requires TEST_DATABASE_URL — see conftest.py (the suite is skipped without it).
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.models.enums import (
    FormProjectType,
    FormVariantStatus,
    MatrixSelectionRule,
    PageObjectType,
    ProjectRole,
    QuestionType,
)
from app.models.form_project import FormProject
from app.models.form_project_member import FormProjectMember
from app.models.form_variant import FormVariant
from app.models.form_variant_page import FormVariantPage
from app.models.matrix_cell_def import MatrixCellDef
from app.models.matrix_col_def import MatrixColDef
from app.models.matrix_object_config import MatrixObjectConfig
from app.models.matrix_row_def import MatrixRowDef
from app.models.page_object import PageObject
from app.models.question_object_config import QuestionObjectConfig
from app.models.question_option import QuestionOption
from app.models.text_object_config import TextObjectConfig
from app.models.user import User
from app.services import form_project_service, form_variant_service


# ---------------------------------------------------------------------------
# Builders — a fully-populated DRAFT variant exercising every typed config
# ---------------------------------------------------------------------------


def _make_project(db, user, *, name="Delete IT project") -> FormProject:
    project = FormProject(
        owner_id=user.id, type=FormProjectType.EXAM, name=name,
    )
    db.add(project)
    db.flush()
    # Membership is the access source of truth: the owner needs an
    # OWNER row so ``require_project_member`` authorizes the delete gates below.
    db.add(
        FormProjectMember(
            project_id=project.id, user_id=user.id, role=ProjectRole.OWNER
        )
    )
    db.flush()
    return project


def _make_variant(
    db, project, user, *, version_number, status=FormVariantStatus.DRAFT
) -> FormVariant:
    variant = FormVariant(
        form_project_id=project.id,
        root_key=f"root-{version_number}",
        name=f"v{version_number}",
        status=status,
        version_number=version_number,
        created_by_user_id=user.id,
    )
    db.add(variant)
    db.flush()
    return variant


def _populate_objects(db, variant) -> dict:
    """Attach one page with a TEXT, a QUESTION (+options) and a MATRIX
    (+rows/cols/cells) object, so the full cascade chain has rows to delete.

    Returns the ids needed to assert the subtree is gone after deletion.
    """
    page = FormVariantPage(form_variant_id=variant.id, page_index=0)
    db.add(page)
    db.flush()

    # TEXT object + config
    text_obj = PageObject(
        form_variant_page_id=page.id, object_type=PageObjectType.TEXT,
        x=0, y=0, width=50, height=20,
    )
    db.add(text_obj)
    db.flush()
    db.add(TextObjectConfig(page_object_id=text_obj.id, text="hello"))

    # QUESTION object + config + options
    q_obj = PageObject(
        form_variant_page_id=page.id, object_type=PageObjectType.QUESTION,
        x=0, y=30, width=200, height=80,
    )
    db.add(q_obj)
    db.flush()
    q_cfg = QuestionObjectConfig(
        page_object_id=q_obj.id,
        question_type=QuestionType.SINGLE_CHOICE,
        question_text="2 + 2 = ?",
    )
    db.add(q_cfg)
    db.flush()
    for i, (txt, correct) in enumerate([("3", False), ("4", True)]):
        db.add(QuestionOption(
            question_object_config_id=q_cfg.id,
            order_index=i, option_text=txt, is_correct=correct,
        ))

    # MATRIX object + config + rows/cols/cells
    m_obj = PageObject(
        form_variant_page_id=page.id, object_type=PageObjectType.MATRIX,
        x=0, y=120, width=200, height=120,
    )
    db.add(m_obj)
    db.flush()
    m_cfg = MatrixObjectConfig(
        page_object_id=m_obj.id, rows_count=2, cols_count=2,
        evaluative=True, selection_rule=MatrixSelectionRule.ONE_PER_ROW,
    )
    db.add(m_cfg)
    db.flush()
    for i in range(2):
        db.add(MatrixRowDef(matrix_object_config_id=m_cfg.id, order_index=i, label=f"R{i}"))
        db.add(MatrixColDef(matrix_object_config_id=m_cfg.id, order_index=i, label=f"C{i}"))
    for r in range(2):
        for c in range(2):
            db.add(MatrixCellDef(
                matrix_object_config_id=m_cfg.id,
                row_index=r, col_index=c, is_correct=(r == c and r == 0),
            ))
    db.flush()

    return {
        "page_id": page.id,
        "object_ids": [text_obj.id, q_obj.id, m_obj.id],
        "q_config_id": q_cfg.id,
        "m_config_id": m_cfg.id,
    }


# ---------------------------------------------------------------------------
# Orphan-row assertions, scoped to the built subtree (rollback-isolation safe)
# ---------------------------------------------------------------------------


def _count(db, model, where) -> int:
    return db.scalar(select(func.count()).select_from(model).where(where)) or 0


def _assert_subtree_gone(db, variant_id, ids) -> None:
    assert _count(db, FormVariantPage, FormVariantPage.form_variant_id == variant_id) == 0
    assert _count(db, PageObject, PageObject.form_variant_page_id == ids["page_id"]) == 0
    obj_ids = ids["object_ids"]
    assert _count(db, TextObjectConfig, TextObjectConfig.page_object_id.in_(obj_ids)) == 0
    assert _count(db, QuestionObjectConfig, QuestionObjectConfig.page_object_id.in_(obj_ids)) == 0
    assert _count(db, MatrixObjectConfig, MatrixObjectConfig.page_object_id.in_(obj_ids)) == 0
    assert _count(db, QuestionOption, QuestionOption.question_object_config_id == ids["q_config_id"]) == 0
    m = ids["m_config_id"]
    assert _count(db, MatrixRowDef, MatrixRowDef.matrix_object_config_id == m) == 0
    assert _count(db, MatrixColDef, MatrixColDef.matrix_object_config_id == m) == 0
    assert _count(db, MatrixCellDef, MatrixCellDef.matrix_object_config_id == m) == 0


# ---------------------------------------------------------------------------
# 1. Delete DRAFT variant -> OK + cascade
# ---------------------------------------------------------------------------
def test_delete_draft_variant_cascades(db, user):
    project = _make_project(db, user)
    variant = _make_variant(db, project, user, version_number=1)
    ids = _populate_objects(db, variant)

    # Sanity: the subtree exists before deletion.
    assert _count(db, MatrixCellDef, MatrixCellDef.matrix_object_config_id == ids["m_config_id"]) == 4

    form_variant_service.delete_variant(db, variant.id, user)

    assert db.get(FormVariant, variant.id) is None
    _assert_subtree_gone(db, variant.id, ids)
    # The parent project is untouched.
    assert db.get(FormProject, project.id) is not None


# ---------------------------------------------------------------------------
# 2. Delete non-DRAFT variant -> 409, nothing removed
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "status", [FormVariantStatus.PUBLISHED, FormVariantStatus.ARCHIVED]
)
def test_delete_non_draft_variant_rejected(db, user, status):
    project = _make_project(db, user)
    variant = _make_variant(db, project, user, version_number=1, status=status)
    ids = _populate_objects(db, variant)

    with pytest.raises(HTTPException) as exc:
        form_variant_service.delete_variant(db, variant.id, user)
    assert exc.value.status_code == 409

    # Nothing deleted: the variant and its objects survive.
    assert db.get(FormVariant, variant.id) is not None
    assert _count(db, PageObject, PageObject.form_variant_page_id == ids["page_id"]) == 3


# ---------------------------------------------------------------------------
# 3. Delete project with ALL variants DRAFT -> OK + cascade
# ---------------------------------------------------------------------------
def test_delete_project_all_draft_cascades(db, user):
    project = _make_project(db, user)
    v1 = _make_variant(db, project, user, version_number=1)
    v2 = _make_variant(db, project, user, version_number=2)
    ids1 = _populate_objects(db, v1)
    ids2 = _populate_objects(db, v2)

    form_project_service.delete_project(db, project.id, user)

    assert db.get(FormProject, project.id) is None
    assert db.get(FormVariant, v1.id) is None
    assert db.get(FormVariant, v2.id) is None
    _assert_subtree_gone(db, v1.id, ids1)
    _assert_subtree_gone(db, v2.id, ids2)


# ---------------------------------------------------------------------------
# 4. Delete project with at least one non-DRAFT variant -> 409, nothing removed
# ---------------------------------------------------------------------------
def test_delete_project_with_non_draft_variant_rejected(db, user):
    project = _make_project(db, user)
    v1 = _make_variant(db, project, user, version_number=1)  # DRAFT
    v2 = _make_variant(
        db, project, user, version_number=2, status=FormVariantStatus.PUBLISHED
    )
    _populate_objects(db, v1)
    _populate_objects(db, v2)

    with pytest.raises(HTTPException) as exc:
        form_project_service.delete_project(db, project.id, user)
    assert exc.value.status_code == 409

    # Nothing deleted: project and both variants survive.
    assert db.get(FormProject, project.id) is not None
    assert db.get(FormVariant, v1.id) is not None
    assert db.get(FormVariant, v2.id) is not None


# ---------------------------------------------------------------------------
# Authorization parity: a non-owner cannot delete (404 to avoid leaking).
# ---------------------------------------------------------------------------
def test_delete_variant_requires_access(db, user):
    project = _make_project(db, user)
    variant = _make_variant(db, project, user, version_number=1)

    intruder = User(
        email=f"intruder-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=user.role,
    )
    db.add(intruder)
    db.flush()

    with pytest.raises(HTTPException) as exc:
        form_variant_service.delete_variant(db, variant.id, intruder)
    assert exc.value.status_code in (403, 404)
    assert db.get(FormVariant, variant.id) is not None
