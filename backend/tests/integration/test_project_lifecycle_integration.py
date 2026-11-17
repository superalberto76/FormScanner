# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed integration tests for the FormProject lifecycle state machine.

The project status is a real state machine governing what may be done on the
project and its variants:
  - transitions are owner-only and validated against a fixed graph (illegal → 409);
  - each state enables/blocks variant + member operations (the operation matrix);
  - archiving cascades PUBLISHED variants → ARCHIVED (DRAFT untouched);
  - status is settable ONLY via transition_project, never via update.

Service-level tests against a real Postgres (the shared ``db`` fixture from
conftest.py). Skipped without TEST_DATABASE_URL.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.models.enums import (
    FormProjectStatus,
    FormProjectType,
    FormVariantStatus,
    UserRole,
)
from app.models.user import User
from app.repositories import form_variant_page_repo, form_variant_repo
from app.schemas.form_project import FormProjectCreate, FormProjectUpdate
from app.schemas.form_variant import FormVariantCreate, FormVariantUpdate
from app.schemas.page_object import TextObjectConfigCreate, TextPageObjectCreate
from app.services import (
    form_project_service,
    form_variant_export_service,
    form_variant_service,
    text_object_service,
)
from app.services.form_variant_export_service import ExportMode

# The detail endpoint computes is_owner (the caller's role) outside the service,
# so the is_owner contract is exercised through the endpoint function directly.
from app.api.v1.endpoints.form_projects import get_form_project


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_user(db, *, role: UserRole = UserRole.USER) -> User:
    u = User(
        email=f"lc-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        role=role,
    )
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def owner(db) -> User:
    return _make_user(db)


def _project(db, owner, name="LC"):
    return form_project_service.create_project(
        db, owner, FormProjectCreate(type=FormProjectType.EXAM, name=name)
    )


def _variant(db, project, user, name="v"):
    """Create a DRAFT variant (auto-provisions one blank page → publishable)."""
    return form_variant_service.create_variant(
        db, project.id, FormVariantCreate(name=name), user
    )


def _t(db, project, target: FormProjectStatus, actor):
    return form_project_service.transition_project(db, project.id, target, actor)


def _project_with_published(db, owner):
    """A DRAFT project carrying one PUBLISHED variant (ready to ACTIVATE)."""
    project = _project(db, owner)
    v = _variant(db, project, owner, "pub")
    form_variant_service.publish_variant(db, v.id, owner)
    return project, v


def _drive_to_active_with_draft(db, owner):
    """ACTIVE project = one PUBLISHED variant + one leftover DRAFT variant.

    The DRAFT variant is created while the project is still DRAFT (the only state
    that allows authoring), then the project is activated.
    """
    project, _pub = _project_with_published(db, owner)
    draft = _variant(db, project, owner, "draft")
    _t(db, project, FormProjectStatus.ACTIVE, owner)
    return project, draft


def _drive_to_closed_with_draft(db, owner):
    project, draft = _drive_to_active_with_draft(db, owner)
    _t(db, project, FormProjectStatus.CLOSED, owner)
    return project, draft


def _drive_to_archived_with_draft(db, owner):
    project, draft = _drive_to_closed_with_draft(db, owner)
    _t(db, project, FormProjectStatus.ARCHIVED, owner)
    return project, draft


def _first_page_id(db, variant_id):
    return form_variant_page_repo.list_for_variant(db, variant_id)[0].id


# ---------------------------------------------------------------------------
# Transition graph
# ---------------------------------------------------------------------------


def test_new_project_starts_draft(db, owner):
    assert _project(db, owner).status is FormProjectStatus.DRAFT


def test_draft_to_active_blocked_without_published_variant(db, owner):
    project = _project(db, owner)
    _variant(db, project, owner)  # DRAFT only — does not count
    with pytest.raises(HTTPException) as ei:
        _t(db, project, FormProjectStatus.ACTIVE, owner)
    assert ei.value.status_code == 409


def test_draft_to_active_allowed_with_published_variant(db, owner):
    project, _ = _project_with_published(db, owner)
    assert _t(db, project, FormProjectStatus.ACTIVE, owner).status is (
        FormProjectStatus.ACTIVE
    )


def test_draft_to_closed_allowed(db, owner):
    project = _project(db, owner)
    assert _t(db, project, FormProjectStatus.CLOSED, owner).status is (
        FormProjectStatus.CLOSED
    )


def test_active_to_closed_allowed(db, owner):
    project, _ = _project_with_published(db, owner)
    _t(db, project, FormProjectStatus.ACTIVE, owner)
    assert _t(db, project, FormProjectStatus.CLOSED, owner).status is (
        FormProjectStatus.CLOSED
    )


def test_active_to_draft_allowed(db, owner):
    project, _ = _project_with_published(db, owner)
    _t(db, project, FormProjectStatus.ACTIVE, owner)
    assert _t(db, project, FormProjectStatus.DRAFT, owner).status is (
        FormProjectStatus.DRAFT
    )


def test_closed_to_draft_allowed(db, owner):
    project = _project(db, owner)
    _t(db, project, FormProjectStatus.CLOSED, owner)
    assert _t(db, project, FormProjectStatus.DRAFT, owner).status is (
        FormProjectStatus.DRAFT
    )


def test_closed_to_archived_allowed(db, owner):
    project = _project(db, owner)
    _t(db, project, FormProjectStatus.CLOSED, owner)
    assert _t(db, project, FormProjectStatus.ARCHIVED, owner).status is (
        FormProjectStatus.ARCHIVED
    )


def test_illegal_draft_to_archived_409(db, owner):
    project = _project(db, owner)
    with pytest.raises(HTTPException) as ei:
        _t(db, project, FormProjectStatus.ARCHIVED, owner)
    assert ei.value.status_code == 409


def test_illegal_active_to_archived_409(db, owner):
    project, _ = _project_with_published(db, owner)
    _t(db, project, FormProjectStatus.ACTIVE, owner)
    with pytest.raises(HTTPException) as ei:
        _t(db, project, FormProjectStatus.ARCHIVED, owner)
    assert ei.value.status_code == 409


def test_archived_is_terminal_409(db, owner):
    project, _ = _drive_to_archived_with_draft(db, owner)
    with pytest.raises(HTTPException) as ei:
        _t(db, project, FormProjectStatus.DRAFT, owner)
    assert ei.value.status_code == 409


def test_transition_non_owner_403(db, owner):
    project = _project(db, owner)
    collab = _make_user(db)
    form_project_service.add_collaborator(db, project.id, collab.id, owner)
    with pytest.raises(HTTPException) as ei:
        _t(db, project, FormProjectStatus.CLOSED, collab)
    assert ei.value.status_code == 403


def test_transition_admin_bypass(db, owner):
    admin = _make_user(db, role=UserRole.ADMIN)
    project = _project(db, owner)
    assert _t(db, project, FormProjectStatus.CLOSED, admin).status is (
        FormProjectStatus.CLOSED
    )


def test_transition_to_same_status_is_idempotent_noop(db, owner):
    project = _project(db, owner)
    assert _t(db, project, FormProjectStatus.DRAFT, owner).status is (
        FormProjectStatus.DRAFT
    )


def test_reopen_does_not_change_variant_status(db, owner):
    project, pub = _project_with_published(db, owner)
    _t(db, project, FormProjectStatus.ACTIVE, owner)
    _t(db, project, FormProjectStatus.CLOSED, owner)
    _t(db, project, FormProjectStatus.DRAFT, owner)  # reopen
    assert form_variant_repo.get(db, pub.id).status is FormVariantStatus.PUBLISHED


# ---------------------------------------------------------------------------
# Operation matrix
# ---------------------------------------------------------------------------


def test_active_blocks_create_variant_409(db, owner):
    project, _ = _drive_to_active_with_draft(db, owner)
    with pytest.raises(HTTPException) as ei:
        _variant(db, project, owner, "new")
    assert ei.value.status_code == 409


def test_active_blocks_update_variant_409(db, owner):
    project, draft = _drive_to_active_with_draft(db, owner)
    with pytest.raises(HTTPException) as ei:
        form_variant_service.update_variant(
            db, draft.id, FormVariantUpdate(name="x"), owner
        )
    assert ei.value.status_code == 409


def test_active_blocks_add_page_409(db, owner):
    project, draft = _drive_to_active_with_draft(db, owner)
    with pytest.raises(HTTPException) as ei:
        form_variant_service.add_page(db, draft.id, owner)
    assert ei.value.status_code == 409


def test_active_blocks_object_create_409(db, owner):
    project, draft = _drive_to_active_with_draft(db, owner)
    page_id = _first_page_id(db, draft.id)
    with pytest.raises(HTTPException) as ei:
        text_object_service.create_text(
            db,
            page_id,
            TextPageObjectCreate(
                x=60, y=60, width=8, height=8,
                config=TextObjectConfigCreate(text="blocked"),
            ),
            owner,
        )
    assert ei.value.status_code == 409


def test_active_allows_publish_draft_variant(db, owner):
    project, draft = _drive_to_active_with_draft(db, owner)
    published = form_variant_service.publish_variant(db, draft.id, owner)
    assert published.status is FormVariantStatus.PUBLISHED


def test_active_allows_delete_draft_variant(db, owner):
    project, draft = _drive_to_active_with_draft(db, owner)
    form_variant_service.delete_variant(db, draft.id, owner)
    assert form_variant_repo.get(db, draft.id) is None


def test_closed_blocks_publish_409(db, owner):
    project, draft = _drive_to_closed_with_draft(db, owner)
    with pytest.raises(HTTPException) as ei:
        form_variant_service.publish_variant(db, draft.id, owner)
    assert ei.value.status_code == 409


def test_closed_allows_delete_draft_variant(db, owner):
    project, draft = _drive_to_closed_with_draft(db, owner)
    form_variant_service.delete_variant(db, draft.id, owner)
    assert form_variant_repo.get(db, draft.id) is None


def test_archived_blocks_delete_variant_409(db, owner):
    project, draft = _drive_to_archived_with_draft(db, owner)
    # The DRAFT variant survived the archive cascade; deletion is still refused.
    assert form_variant_repo.get(db, draft.id).status is FormVariantStatus.DRAFT
    with pytest.raises(HTTPException) as ei:
        form_variant_service.delete_variant(db, draft.id, owner)
    assert ei.value.status_code == 409


def test_archived_blocks_member_add_409(db, owner):
    project, _ = _drive_to_archived_with_draft(db, owner)
    other = _make_user(db)
    with pytest.raises(HTTPException) as ei:
        form_project_service.add_collaborator(db, project.id, other.id, owner)
    assert ei.value.status_code == 409


def test_export_works_in_archived(db, owner):
    project, draft = _drive_to_archived_with_draft(db, owner)
    # No project-state gate on export — both the cheap metadata path and the full
    # PDF render succeed in ARCHIVED.
    meta = form_variant_export_service.get_preview_metadata(db, draft.id, owner)
    assert meta["page_count"] >= 1
    pdf_bytes, filename = form_variant_export_service.export_pdf(
        db, draft.id, owner, ExportMode.STUDENT
    )
    assert pdf_bytes[:4] == b"%PDF"
    assert isinstance(filename, str)


# ---------------------------------------------------------------------------
# Archive cascade
# ---------------------------------------------------------------------------


def test_archive_cascade_published_archived_draft_untouched(db, owner):
    project = _project(db, owner)
    pub = _variant(db, project, owner, "pub")
    form_variant_service.publish_variant(db, pub.id, owner)
    drf = _variant(db, project, owner, "drf")  # stays DRAFT

    _t(db, project, FormProjectStatus.ACTIVE, owner)
    _t(db, project, FormProjectStatus.CLOSED, owner)
    _t(db, project, FormProjectStatus.ARCHIVED, owner)

    assert form_variant_repo.get(db, pub.id).status is FormVariantStatus.ARCHIVED
    assert form_variant_repo.get(db, drf.id).status is FormVariantStatus.DRAFT


# ---------------------------------------------------------------------------
# Variant detail contract + the closed status backdoor
# ---------------------------------------------------------------------------


def test_get_variant_detail_returns_parent_project_status(db, owner):
    project = _project(db, owner)
    v = _variant(db, project, owner)
    assert (
        form_variant_service.get_variant_detail(db, v.id, owner).form_project_status
        is FormProjectStatus.DRAFT
    )
    # Reflects the live project status after a transition.
    form_variant_service.publish_variant(db, v.id, owner)
    _t(db, project, FormProjectStatus.ACTIVE, owner)
    assert (
        form_variant_service.get_variant_detail(db, v.id, owner).form_project_status
        is FormProjectStatus.ACTIVE
    )


def test_form_project_update_cannot_set_status(db, owner):
    # The field is gone from the schema, so status can never be patched.
    assert "status" not in FormProjectUpdate.model_fields
    project = _project(db, owner)
    form_project_service.update_project(
        db, project.id, FormProjectUpdate(name="renamed"), owner
    )
    assert form_project_service.get_project(db, project.id, owner).status is (
        FormProjectStatus.DRAFT
    )


# ---------------------------------------------------------------------------
# Project detail contract: is_owner (drives the FE owner-only lifecycle controls)
# ---------------------------------------------------------------------------


def test_get_project_detail_is_owner_true_for_owner(db, owner):
    project = _project(db, owner)
    read = get_form_project(project_id=project.id, db=db, user=owner)
    assert read.is_owner is True


def test_get_project_detail_is_owner_false_for_collaborator(db, owner):
    project = _project(db, owner)
    collab = _make_user(db)
    form_project_service.add_collaborator(db, project.id, collab.id, owner)
    read = get_form_project(project_id=project.id, db=db, user=collab)
    assert read.is_owner is False


def test_get_project_detail_is_owner_false_for_admin_non_member(db, owner):
    # An admin can view (and act on) any project but is not a member → is_owner
    # False; they still transition via the endpoint's admin bypass.
    project = _project(db, owner)
    admin = _make_user(db, role=UserRole.ADMIN)
    read = get_form_project(project_id=project.id, db=db, user=admin)
    assert read.is_owner is False
