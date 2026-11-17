# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""FormProject CRUD endpoints.

Mounted at /form-projects.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.deps import get_db, require_user
from app.models.enums import ProjectRole
from app.models.user import User
from app.repositories import form_project_member_repo
from app.schemas.form_project import (
    FormProjectCreate,
    FormProjectListItem,
    FormProjectMemberRead,
    FormProjectRead,
    FormProjectTransition,
    FormProjectUpdate,
    MemberAddRequest,
    MemberRoleUpdate,
    ScanReadiness,
)
from app.services import form_project_service

router = APIRouter()


@router.get("", response_model=list[FormProjectListItem])
def list_form_projects(
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """List form projects visible to the current user.

    A user sees every project they are a member of (owner or collaborator);
    ADMIN sees all.
    """
    return form_project_service.list_projects(db, user)


@router.post("", response_model=FormProjectRead, status_code=status.HTTP_201_CREATED)
def create_form_project(
    data: FormProjectCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Create a new form project owned by the current user (status=DRAFT)."""
    project = form_project_service.create_project(db, user, data)
    return FormProjectRead.model_validate(project)


@router.get("/{project_id}", response_model=FormProjectRead)
def get_form_project(
    project_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Get a single form project (any member or admin).

    Carries ``is_owner`` (the CALLER's role on THIS project) so the FE can show
    owner-only lifecycle controls. An admin viewing a project they are not a
    member of gets ``is_owner=False`` — they still act via the transition
    endpoint's admin bypass.
    """
    project = form_project_service.get_project(db, project_id, user)
    member = form_project_member_repo.get(db, project_id, user.id)
    read = FormProjectRead.model_validate(project)
    read.is_owner = member is not None and member.role is ProjectRole.OWNER
    return read


@router.get("/{project_id}/scan-readiness", response_model=ScanReadiness)
def get_form_project_scan_readiness(
    project_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Whether this project's published variants are coherently configured for a
    scan batch.

    Always 200; ready=False carries a human-readable reason. Read-only: the
    same rule refuses batch creation and uploads with the structured 409
    ``NOT_SCAN_READY``, whose message is this reason.
    Any member may query it (admin bypasses); non-members get 403.
    """
    return form_project_service.get_scan_readiness(db, project_id, user)


@router.patch("/{project_id}", response_model=FormProjectRead)
def update_form_project(
    project_id: uuid.UUID,
    data: FormProjectUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Update name/description/event_date (any member or admin).

    Status is NOT settable here — lifecycle moves go through POST .../transition.
    """
    project = form_project_service.update_project(db, project_id, data, user)
    return FormProjectRead.model_validate(project)


@router.post("/{project_id}/transition", response_model=FormProjectRead)
def transition_form_project(
    project_id: uuid.UUID,
    data: FormProjectTransition,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Move a project to a new lifecycle status (owner-only, graph-validated).

    Illegal transitions and unmet gates (e.g. DRAFT→ACTIVE with no PUBLISHED
    variant) return 409; non-owners 403. Archiving cascades PUBLISHED variants to
    ARCHIVED. The owner gate is enforced inside the service.
    """
    project = form_project_service.transition_project(
        db, project_id, data.target_status, user
    )
    return FormProjectRead.model_validate(project)


@router.get("/{project_id}/members", response_model=list[FormProjectMemberRead])
def list_form_project_members(
    project_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """List a project's members (owners first).

    Any member — including collaborators — may view the roster; admin bypasses.
    """
    return form_project_service.list_members(db, project_id, user)


@router.post(
    "/{project_id}/members",
    response_model=FormProjectMemberRead,
    status_code=status.HTTP_201_CREATED,
)
def add_form_project_member(
    project_id: uuid.UUID,
    data: MemberAddRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Add an existing user as a COLLABORATOR (owner-only).

    409 if already a member; 404 if the user does not exist.
    """
    return form_project_service.add_collaborator(db, project_id, data.user_id, user)


@router.patch(
    "/{project_id}/members/{user_id}", response_model=FormProjectMemberRead
)
def update_form_project_member_role(
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    data: MemberRoleUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Promote/demote a member (owner-only).

    Never demote yourself (400) or the last owner (409).
    """
    return form_project_service.set_member_role(
        db, project_id, user_id, data.role, user
    )


@router.delete(
    "/{project_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT
)
def remove_form_project_member(
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Remove a member (owner-only).

    Never remove yourself as owner (400) or the last owner (409).
    """
    form_project_service.remove_member(db, project_id, user_id, user)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_form_project(
    project_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Delete a form project.

    Safe hard delete: returns 409 if the project still has variants.
    (The model has no soft-delete flag yet; see service docstring.)
    """
    form_project_service.delete_project(db, project_id, user)
