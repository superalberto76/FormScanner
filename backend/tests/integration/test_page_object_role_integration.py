# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for PageObject special scanning roles.

Covers the identification contract against real Postgres (the models use
postgresql.UUID + native enums, so SQLite is not an option):

  1. assign_role binds an allowed object type and the typed read + enriched page
     list then expose ``role``;
  2. RESPONDENT_IDENTIFIER accepts CODE / MATRIX / FREE_TEXT_AREA;
  3. an object type not allowed for the role is refused (409);
  4. one role per object: a second role on the same object is refused (409);
  5. one object per role, per variant: the same role on a second object is
     refused (409), naming the current holder;
  6. remove_role frees the object (and lets it be re-assigned); removing when
     there is no binding is 404;
  7. deleting an object that still holds a role is blocked (409); so is deleting
     a whole linked group when even one member holds a role;
  8. list_variant_role_bindings returns the variant's bindings and enforces
     member access;
  9. every mutation requires a DRAFT variant.

NOTE ON SETUP: membership is the access source of truth, so these tests add an
OWNER ``FormProjectMember`` row so ``require_project_member`` passes.

Requires TEST_DATABASE_URL — see conftest.py (the suite is skipped without it).
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.models.code_object_config import CodeObjectConfig
from app.models.enums import (
    CodeType,
    FormProjectType,
    FormVariantStatus,
    MatrixSelectionRule,
    PageObjectRole,
    PageObjectType,
    ProjectRole,
    UserRole,
)
from app.models.form_project import FormProject
from app.models.form_project_member import FormProjectMember
from app.models.form_variant import FormVariant
from app.models.form_variant_page import FormVariantPage
from app.models.free_text_area_config import FreeTextAreaConfig
from app.models.matrix_object_config import MatrixObjectConfig
from app.models.page_object import PageObject
from app.models.text_object_config import TextObjectConfig
from app.models.user import User
from app.repositories import form_variant_role_binding_repo
from app.schemas.page_object import PageObjectRoleRead
from app.services import page_object_role_service, page_object_service


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_user(db, *, role=UserRole.USER) -> User:
    u = User(
        email=f"role-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=role,
    )
    db.add(u)
    db.flush()
    return u


def _make_project(db, owner, *, name="Role IT project") -> FormProject:
    """A DRAFT project with an OWNER membership row for *owner* (membership gate)."""
    project = FormProject(owner_id=owner.id, type=FormProjectType.EXAM, name=name)
    db.add(project)
    db.flush()
    db.add(
        FormProjectMember(
            project_id=project.id, user_id=owner.id, role=ProjectRole.OWNER
        )
    )
    db.flush()
    return project


def _make_variant(db, project, owner) -> FormVariant:
    variant = FormVariant(
        form_project_id=project.id,
        root_key="root",
        name="v1",
        status=FormVariantStatus.DRAFT,
        version_number=1,
        created_by_user_id=owner.id,
    )
    db.add(variant)
    db.flush()
    return variant


def _add_page(db, variant, page_index=0) -> FormVariantPage:
    pg = FormVariantPage(form_variant_id=variant.id, page_index=page_index)
    db.add(pg)
    db.flush()
    return pg


def _setup(db, owner):
    """project (+OWNER member) -> DRAFT variant -> first page."""
    project = _make_project(db, owner)
    variant = _make_variant(db, project, owner)
    page = _add_page(db, variant)
    return project, variant, page


def _add_code_object(
    db, page_id, *, label="C", value="X1", shared_group_id=None, shared_across_pages=False
) -> PageObject:
    obj = PageObject(
        form_variant_page_id=page_id,
        object_type=PageObjectType.CODE,
        x=0, y=0, width=50, height=50, label=label,
        shared_group_id=shared_group_id,
        shared_across_pages=shared_across_pages,
    )
    db.add(obj)
    db.flush()
    db.add(CodeObjectConfig(page_object_id=obj.id, code_type=CodeType.QR, value=value))
    db.flush()
    return obj


def _add_text_object(db, page_id, *, label="T", text="hi") -> PageObject:
    obj = PageObject(
        form_variant_page_id=page_id,
        object_type=PageObjectType.TEXT,
        x=0, y=0, width=50, height=20, label=label,
    )
    db.add(obj)
    db.flush()
    db.add(TextObjectConfig(page_object_id=obj.id, text=text))
    db.flush()
    return obj


def _add_free_text_object(db, page_id, *, label="F") -> PageObject:
    obj = PageObject(
        form_variant_page_id=page_id,
        object_type=PageObjectType.FREE_TEXT_AREA,
        x=0, y=0, width=100, height=40, label=label,
    )
    db.add(obj)
    db.flush()
    db.add(FreeTextAreaConfig(page_object_id=obj.id))
    db.flush()
    return obj


def _add_matrix_object(db, page_id, *, label="M") -> PageObject:
    obj = PageObject(
        form_variant_page_id=page_id,
        object_type=PageObjectType.MATRIX,
        x=0, y=0, width=200, height=120, label=label,
    )
    db.add(obj)
    db.flush()
    db.add(MatrixObjectConfig(
        page_object_id=obj.id,
        rows_count=1, cols_count=1,
        evaluative=False, selection_rule=MatrixSelectionRule.ONE_PER_ROW,
    ))
    db.flush()
    return obj


def _reload(db, object_id) -> PageObject | None:
    db.expire_all()
    return db.get(PageObject, object_id)


# ---------------------------------------------------------------------------
# 1. assign happy path — binding created; typed read + enriched list expose role
# ---------------------------------------------------------------------------
def test_assign_role_creates_binding_and_exposes_role(db, user):
    _, variant, page = _setup(db, user)
    obj = _add_code_object(db, page.id, label="ident")
    # VARIANT_IDENTIFIER requires a shared object.
    page_object_service.share_object(db, obj.id, user)

    read = page_object_role_service.assign_role(
        db, obj.id, PageObjectRole.VARIANT_IDENTIFIER, user
    )
    assert read.role is PageObjectRole.VARIANT_IDENTIFIER

    binding = form_variant_role_binding_repo.get_by_page_object(db, obj.id)
    assert binding is not None
    assert binding.role is PageObjectRole.VARIANT_IDENTIFIER
    assert binding.form_variant_id == variant.id

    # The enriched page list exposes role too (via the eager-loaded relationship).
    items = page_object_service.list_objects(db, page.id, user)
    assert {i.id: i.role for i in items}[obj.id] is PageObjectRole.VARIANT_IDENTIFIER


# ---------------------------------------------------------------------------
# 2. RESPONDENT_IDENTIFIER accepts CODE / MATRIX / FREE_TEXT_AREA
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("builder", ["code", "matrix", "free_text"])
def test_respondent_identifier_allowed_types(db, user, builder):
    _, variant, page = _setup(db, user)
    obj = {
        "code": _add_code_object,
        "matrix": _add_matrix_object,
        "free_text": _add_free_text_object,
    }[builder](db, page.id)

    read = page_object_role_service.assign_role(
        db, obj.id, PageObjectRole.RESPONDENT_IDENTIFIER, user
    )
    assert read.role is PageObjectRole.RESPONDENT_IDENTIFIER


# ---------------------------------------------------------------------------
# 3. disallowed type -> 409
# ---------------------------------------------------------------------------
def test_variant_identifier_rejects_non_code(db, user):
    _, variant, page = _setup(db, user)
    free_text = _add_free_text_object(db, page.id)

    with pytest.raises(HTTPException) as exc:
        page_object_role_service.assign_role(
            db, free_text.id, PageObjectRole.VARIANT_IDENTIFIER, user
        )
    assert exc.value.status_code == 409
    assert form_variant_role_binding_repo.get_by_page_object(db, free_text.id) is None


def test_respondent_identifier_rejects_text(db, user):
    _, variant, page = _setup(db, user)
    text = _add_text_object(db, page.id)

    with pytest.raises(HTTPException) as exc:
        page_object_role_service.assign_role(
            db, text.id, PageObjectRole.RESPONDENT_IDENTIFIER, user
        )
    assert exc.value.status_code == 409


# ---------------------------------------------------------------------------
# 4. one role per object -> second (different) role is refused
# ---------------------------------------------------------------------------
def test_object_cannot_hold_two_roles(db, user):
    _, variant, page = _setup(db, user)
    obj = _add_code_object(db, page.id)
    # VARIANT_IDENTIFIER requires a shared object.
    page_object_service.share_object(db, obj.id, user)

    page_object_role_service.assign_role(
        db, obj.id, PageObjectRole.VARIANT_IDENTIFIER, user
    )
    with pytest.raises(HTTPException) as exc:
        page_object_role_service.assign_role(
            db, obj.id, PageObjectRole.RESPONDENT_IDENTIFIER, user
        )
    assert exc.value.status_code == 409
    # The original binding is untouched.
    assert (
        form_variant_role_binding_repo.get_by_page_object(db, obj.id).role
        is PageObjectRole.VARIANT_IDENTIFIER
    )


# ---------------------------------------------------------------------------
# 5. one object per role, per variant -> the same role on a second object refused
# ---------------------------------------------------------------------------
def test_role_cannot_be_held_by_two_objects(db, user):
    _, variant, page = _setup(db, user)
    first = _add_code_object(db, page.id, label="first")
    second = _add_code_object(db, page.id, label="second", value="X2")
    # VARIANT_IDENTIFIER requires a shared object: share both so
    # the second assign is refused for the intended reason (role already held),
    # not for a missing-share reason.
    page_object_service.share_object(db, first.id, user)
    page_object_service.share_object(db, second.id, user)

    page_object_role_service.assign_role(
        db, first.id, PageObjectRole.VARIANT_IDENTIFIER, user
    )
    with pytest.raises(HTTPException) as exc:
        page_object_role_service.assign_role(
            db, second.id, PageObjectRole.VARIANT_IDENTIFIER, user
        )
    assert exc.value.status_code == 409
    # The message names the current holder so the user can free it first.
    assert str(first.id) in exc.value.detail
    assert form_variant_role_binding_repo.get_by_page_object(db, second.id) is None


def test_same_role_allowed_across_different_variants(db, user):
    """The (form_variant_id, role) uniqueness is per-variant, not global."""
    project = _make_project(db, user)
    v1 = _make_variant(db, project, user)
    p1 = _add_page(db, v1)
    v2 = FormVariant(
        form_project_id=project.id, root_key="root", name="v2",
        status=FormVariantStatus.DRAFT, version_number=2, created_by_user_id=user.id,
    )
    db.add(v2)
    db.flush()
    p2 = _add_page(db, v2)

    o1 = _add_code_object(db, p1.id)
    o2 = _add_code_object(db, p2.id, value="X2")
    # VARIANT_IDENTIFIER requires a shared object.
    page_object_service.share_object(db, o1.id, user)
    page_object_service.share_object(db, o2.id, user)

    page_object_role_service.assign_role(db, o1.id, PageObjectRole.VARIANT_IDENTIFIER, user)
    # Same role on an object of a DIFFERENT variant is fine.
    read = page_object_role_service.assign_role(
        db, o2.id, PageObjectRole.VARIANT_IDENTIFIER, user
    )
    assert read.role is PageObjectRole.VARIANT_IDENTIFIER


# ---------------------------------------------------------------------------
# 6. remove frees the object; removing a non-existent binding is 404
# ---------------------------------------------------------------------------
def test_remove_role_frees_object_and_allows_reassign(db, user):
    _, variant, page = _setup(db, user)
    obj = _add_code_object(db, page.id)
    other = _add_code_object(db, page.id, value="X2")
    # VARIANT_IDENTIFIER requires a shared object.
    page_object_service.share_object(db, obj.id, user)
    page_object_service.share_object(db, other.id, user)

    page_object_role_service.assign_role(db, obj.id, PageObjectRole.VARIANT_IDENTIFIER, user)
    page_object_role_service.remove_role(db, obj.id, user)
    assert form_variant_role_binding_repo.get_by_page_object(db, obj.id) is None

    # The role is now free — another object can take it.
    page_object_role_service.assign_role(db, other.id, PageObjectRole.VARIANT_IDENTIFIER, user)
    assert (
        form_variant_role_binding_repo.get_by_page_object(db, other.id).role
        is PageObjectRole.VARIANT_IDENTIFIER
    )


def test_remove_role_without_binding_is_404(db, user):
    _, variant, page = _setup(db, user)
    obj = _add_code_object(db, page.id)

    with pytest.raises(HTTPException) as exc:
        page_object_role_service.remove_role(db, obj.id, user)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# 7. delete is blocked while a role is held (single object AND whole linked group)
# ---------------------------------------------------------------------------
def test_delete_object_with_role_is_blocked(db, user):
    _, variant, page = _setup(db, user)
    obj = _add_code_object(db, page.id)
    # VARIANT_IDENTIFIER requires a shared object.
    page_object_service.share_object(db, obj.id, user)
    page_object_role_service.assign_role(db, obj.id, PageObjectRole.VARIANT_IDENTIFIER, user)

    with pytest.raises(HTTPException) as exc:
        page_object_service.delete_object(db, obj.id, user)
    assert exc.value.status_code == 409
    # Nothing was deleted.
    assert _reload(db, obj.id) is not None
    assert form_variant_role_binding_repo.get_by_page_object(db, obj.id) is not None


def test_delete_linked_group_blocked_if_any_member_has_role(db, user):
    _, variant, page1 = _setup(db, user)
    page2 = _add_page(db, variant, page_index=1)
    gid = uuid.uuid4()

    m1 = _add_code_object(db, page1.id, label="m1", shared_group_id=gid, shared_across_pages=True)
    m2 = _add_code_object(db, page2.id, label="m2", value="X2", shared_group_id=gid, shared_across_pages=True)
    # Only m1 holds the role; deleting m2 expands to the whole group and must fail.
    page_object_role_service.assign_role(db, m1.id, PageObjectRole.VARIANT_IDENTIFIER, user)

    with pytest.raises(HTTPException) as exc:
        page_object_service.delete_object(db, m2.id, user)
    assert exc.value.status_code == 409
    assert str(m1.id) in exc.value.detail
    # The whole group survives.
    assert _reload(db, m1.id) is not None
    assert _reload(db, m2.id) is not None


# ---------------------------------------------------------------------------
# 8. list_variant_role_bindings — returns rows; enforces member access
# ---------------------------------------------------------------------------
def test_list_variant_role_bindings(db, user):
    _, variant, page = _setup(db, user)
    code = _add_code_object(db, page.id)
    matrix = _add_matrix_object(db, page.id)
    # VARIANT_IDENTIFIER requires a shared object; the
    # RESPONDENT_IDENTIFIER on the matrix carries no such requirement.
    page_object_service.share_object(db, code.id, user)
    page_object_role_service.assign_role(db, code.id, PageObjectRole.VARIANT_IDENTIFIER, user)
    page_object_role_service.assign_role(db, matrix.id, PageObjectRole.RESPONDENT_IDENTIFIER, user)

    bindings = page_object_role_service.list_variant_role_bindings(db, variant.id, user)
    assert {b.role for b in bindings} == {
        PageObjectRole.VARIANT_IDENTIFIER,
        PageObjectRole.RESPONDENT_IDENTIFIER,
    }
    assert {b.page_object_id for b in bindings} == {code.id, matrix.id}


def test_list_variant_role_bindings_exposes_object_label_and_page_index(db, user):
    """The read enriches each binding with the bound object's label and the
    index of the page that hosts it, read straight off the
    FormVariantRoleBinding properties via the response schema."""
    _, variant, page0 = _setup(db, user)
    page1 = _add_page(db, variant, page_index=1)
    # A labelled RESPONDENT_IDENTIFIER on the SECOND page, so page_index is
    # meaningfully non-zero (not merely the default 0). RESPONDENT_IDENTIFIER
    # on a CODE carries no share requirement.
    obj = _add_code_object(db, page1.id, label="respondent code")
    page_object_role_service.assign_role(
        db, obj.id, PageObjectRole.RESPONDENT_IDENTIFIER, user
    )

    bindings = page_object_role_service.list_variant_role_bindings(db, variant.id, user)
    assert len(bindings) == 1
    binding = bindings[0]
    # Read-only properties on the ORM row...
    assert binding.object_label == "respondent code"
    assert binding.page_index == 1
    # ...and the response schema surfaces them verbatim via from_attributes.
    read = PageObjectRoleRead.model_validate(binding)
    assert read.object_label == "respondent code"
    assert read.page_index == 1
    assert read.page_object_id == obj.id


def test_list_variant_role_bindings_object_label_none_when_unset(db, user):
    """No text fallback: an object without a label serializes object_label=None
    (the FE decides how to present it)."""
    _, variant, page = _setup(db, user)
    obj = _add_code_object(db, page.id, label=None)
    page_object_role_service.assign_role(
        db, obj.id, PageObjectRole.RESPONDENT_IDENTIFIER, user
    )

    bindings = page_object_role_service.list_variant_role_bindings(db, variant.id, user)
    assert len(bindings) == 1
    binding = bindings[0]
    assert binding.object_label is None
    assert binding.page_index == 0
    read = PageObjectRoleRead.model_validate(binding)
    assert read.object_label is None
    assert read.page_index == 0


def test_list_variant_role_bindings_exposes_shared_group_id(db, user):
    """A binding on a shared object exposes its shared_group_id, so the FE
    can tell whether a shared sibling holds VARIANT_IDENTIFIER to enable
    "include page number" in preview. Read straight off the
    FormVariantRoleBinding property via the response schema."""
    _, variant, page = _setup(db, user)
    gid = uuid.uuid4()
    obj = _add_code_object(
        db, page.id, label="shared code",
        shared_group_id=gid, shared_across_pages=True,
    )
    # RESPONDENT_IDENTIFIER on a CODE carries no share requirement, so this
    # isolates the shared_group_id enrichment from the VARIANT_IDENTIFIER share gate.
    page_object_role_service.assign_role(
        db, obj.id, PageObjectRole.RESPONDENT_IDENTIFIER, user
    )

    bindings = page_object_role_service.list_variant_role_bindings(db, variant.id, user)
    assert len(bindings) == 1
    binding = bindings[0]
    # Read-only property on the ORM row...
    assert binding.shared_group_id == gid
    # ...and the response schema surfaces it verbatim via from_attributes.
    read = PageObjectRoleRead.model_validate(binding)
    assert read.shared_group_id == gid


def test_list_variant_role_bindings_shared_group_id_none_when_not_shared(db, user):
    """An ordinary, page-local object (not shared) serializes shared_group_id=None."""
    _, variant, page = _setup(db, user)
    obj = _add_code_object(db, page.id, label="plain code")
    page_object_role_service.assign_role(
        db, obj.id, PageObjectRole.RESPONDENT_IDENTIFIER, user
    )

    bindings = page_object_role_service.list_variant_role_bindings(db, variant.id, user)
    assert len(bindings) == 1
    binding = bindings[0]
    assert binding.shared_group_id is None
    read = PageObjectRoleRead.model_validate(binding)
    assert read.shared_group_id is None


def test_list_variant_role_bindings_denies_non_member(db, user):
    _, variant, page = _setup(db, user)
    stranger = _make_user(db)  # not a member of the project

    with pytest.raises(HTTPException) as exc:
        page_object_role_service.list_variant_role_bindings(db, variant.id, stranger)
    assert exc.value.status_code == 403


def test_list_variant_role_bindings_missing_variant_is_404(db, user):
    with pytest.raises(HTTPException) as exc:
        page_object_role_service.list_variant_role_bindings(db, uuid.uuid4(), user)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# 9. mutations require a DRAFT variant
# ---------------------------------------------------------------------------
def test_assign_role_requires_draft_variant(db, user):
    _, variant, page = _setup(db, user)
    obj = _add_code_object(db, page.id)
    variant.status = FormVariantStatus.PUBLISHED
    db.flush()

    with pytest.raises(HTTPException) as exc:
        page_object_role_service.assign_role(
            db, obj.id, PageObjectRole.VARIANT_IDENTIFIER, user
        )
    assert exc.value.status_code == 409
    assert form_variant_role_binding_repo.get_by_page_object(db, obj.id) is None
