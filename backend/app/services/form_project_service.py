# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""FormProject lifecycle service — create, list, detail, update, delete.

Authorization model (role-based membership):
  - Access is decided ONLY by ``FormProjectMember`` rows (the source of truth);
    ``owner_id`` is just the primary/creator marker, NOT an access check.
  - MEMBER-level (any role: OWNER or COLLABORATOR) — view, author, export.
  - OWNER-level — ownership actions (delete, member management).
  - ADMIN bypasses both checks (sees/does everything).
"""
from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.enums import (
    FormProjectStatus,
    PageObjectRole,
    ProjectRole,
    UserRole,
)
from app.models.form_project import FormProject
from app.models.form_project_member import FormProjectMember
from app.models.user import User
from app.repositories import (
    code_object_repo,
    form_project_member_repo,
    form_project_repo,
    form_variant_repo,
    form_variant_role_binding_repo,
    image_object_repo,
    user_repo,
)
from app.schemas.form_project import (
    FormProjectCreate,
    FormProjectListItem,
    FormProjectMemberRead,
    FormProjectUpdate,
    ScanReadiness,
)
from app.services import audit_service, storage_service


# ---------------------------------------------------------------------------
# Authorization primitives — the single gate for project/variant
# access. Membership is the source of truth; admins bypass.
# ---------------------------------------------------------------------------


def require_project_member(
    db: Session, project_id: uuid.UUID, user: User
) -> FormProject:
    """Fetch a project the user may access as a MEMBER, or raise 404/403.

    404 if the project does not exist; 403 if the caller is neither a member
    (any role) nor an admin. Same signature as the former ``get_owned_or_404``.
    """
    project = form_project_repo.get(db, project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Form project not found"
        )
    if user.role is UserRole.ADMIN:
        return project
    if form_project_member_repo.get(db, project_id, user.id) is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not a member of this project",
        )
    return project


def require_project_owner(
    db: Session, project_id: uuid.UUID, user: User
) -> FormProject:
    """Fetch a project the user may act on as an OWNER, or raise 404/403.

    404 if the project does not exist; 403 if the caller is not an OWNER member
    (collaborators are rejected) and not an admin.
    """
    project = form_project_repo.get(db, project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Form project not found"
        )
    if user.role is UserRole.ADMIN:
        return project
    member = form_project_member_repo.get(db, project_id, user.id)
    if member is None or member.role is not ProjectRole.OWNER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a project owner may perform this action",
        )
    return project


def require_any_project_owner(db: Session, user: User) -> None:
    """Pass iff *user* is an OWNER of at least one project, or is an admin.

    Gates user-search: any project owner (not just admins) may look up existing
    users to add. 403 otherwise.
    """
    if user.role is UserRole.ADMIN:
        return
    if not form_project_member_repo.has_owner_membership(db, user.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a project owner may search users",
        )


# ---------------------------------------------------------------------------
# Lifecycle state machine — the single source of truth for what a project's
# status PERMITS. Kept in this module (not variant/page_object services) so both
# of those can import the gating helpers without an import cycle: this module
# imports neither of them.
# ---------------------------------------------------------------------------


# The ONLY legal status moves. Everything else is a 409. ARCHIVED is terminal.
_ALLOWED_TRANSITIONS: dict[FormProjectStatus, set[FormProjectStatus]] = {
    FormProjectStatus.DRAFT: {FormProjectStatus.ACTIVE, FormProjectStatus.CLOSED},
    FormProjectStatus.ACTIVE: {FormProjectStatus.CLOSED, FormProjectStatus.DRAFT},
    FormProjectStatus.CLOSED: {FormProjectStatus.DRAFT, FormProjectStatus.ARCHIVED},
    FormProjectStatus.ARCHIVED: set(),
}


def assert_project_editable(project: FormProject) -> None:
    """Authoring gate: create/modify variants, pages, objects (DRAFT only)."""
    if project.status is not FormProjectStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Project is {project.status.value}; "
                "content can only be edited while DRAFT."
            ),
        )


def assert_can_publish(project: FormProject) -> None:
    """Publish gate: a DRAFT variant may be published only in DRAFT or ACTIVE."""
    if project.status not in (FormProjectStatus.DRAFT, FormProjectStatus.ACTIVE):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Project is {project.status.value}; "
                "variants can be published only in DRAFT or ACTIVE."
            ),
        )


def assert_project_active_for_scanning(project: FormProject) -> None:
    """Scanning gate: a batch of scanned sheets may only be uploaded while ACTIVE.

    Authoring happens in DRAFT and collection happens in ACTIVE, so ingestion is
    refused in every other state. Re-checked on each upload, not just
    at batch creation — the project can be moved out of ACTIVE in between.
    """
    if project.status is not FormProjectStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Project is {project.status.value}; submission "
                "batches can only be created while ACTIVE."
            ),
        )


def assert_variant_deletable(project: FormProject) -> None:
    """Housekeeping gate: a DRAFT variant may be deleted unless ARCHIVED."""
    if project.status is FormProjectStatus.ARCHIVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Project is ARCHIVED; nothing can be deleted.",
        )


def assert_members_manageable(project: FormProject) -> None:
    """Member-management gate: add/remove/promote/demote, disabled in ARCHIVED."""
    if project.status is FormProjectStatus.ARCHIVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Project is ARCHIVED; member management is disabled.",
        )


def transition_project(
    db: Session,
    project_id: uuid.UUID,
    target_status: FormProjectStatus,
    user: User,
) -> FormProject:
    """Move a project to *target_status* — owner-only, graph-validated.

    Validates the move against ``_ALLOWED_TRANSITIONS`` (illegal jumps → 409),
    enforces the DRAFT→ACTIVE gate (≥1 PUBLISHED variant), and on →ARCHIVED runs
    the archive cascade (PUBLISHED variants → ARCHIVED; DRAFT untouched). A no-op
    request (already in *target_status*) returns the project unchanged.

    Status is settable ONLY here — never via ``update_project``.
    """
    project = require_project_owner(db, project_id, user)  # OWNER-only (admin bypass)
    current = project.status

    if target_status == current:
        return project  # idempotent no-op

    if target_status not in _ALLOWED_TRANSITIONS[current]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Illegal transition {current.value} -> {target_status.value}."
            ),
        )

    if current is FormProjectStatus.DRAFT and target_status is FormProjectStatus.ACTIVE:
        if form_variant_repo.count_published_variants(db, project_id) < 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Activate requires at least one PUBLISHED variant.",
            )

    archived = 0
    if target_status is FormProjectStatus.ARCHIVED:
        # PUBLISHED -> ARCHIVED; DRAFT variants stay DRAFT (the archive cascade).
        archived = form_variant_repo.archive_published_variants(db, project_id)

    project.status = target_status
    db.flush()
    audit_service.log_form_project_updated(
        db,
        user.id,
        project_id,
        {
            "transition": f"{current.value}->{target_status.value}",
            "variants_archived": archived,
        },
    )
    db.commit()
    db.refresh(project)
    return project


def _member_read(
    member: FormProjectMember, member_user: User
) -> FormProjectMemberRead:
    """Map a (membership, user) pair to the read schema (DRY across endpoints)."""
    return FormProjectMemberRead(
        user_id=member.user_id,
        first_name=member_user.first_name,
        last_name=member_user.last_name,
        role=member.role,
        has_avatar=member_user.has_avatar,
    )


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def create_project(
    db: Session, owner: User, data: FormProjectCreate
) -> FormProject:
    project = FormProject(
        owner_id=owner.id,  # primary/creator marker (first avatar in the stack)
        type=data.type,
        name=data.name,
        description=data.description,
        event_date=data.event_date,
        # status defaults to DRAFT on the model.
    )
    form_project_repo.add(db, project)  # flush populates project.id

    # Seed the creator as the project's OWNER member — membership is the access
    # source of truth. Same transaction as the project insert.
    form_project_member_repo.add(
        db,
        FormProjectMember(
            project_id=project.id,
            user_id=owner.id,
            role=ProjectRole.OWNER,
        ),
    )

    audit_service.log_form_project_created(
        db, owner.id, project.id, {"type": data.type.value}
    )
    db.commit()
    db.refresh(project)
    return project


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


def list_projects(db: Session, user: User) -> list[FormProjectListItem]:
    """List projects visible to *user*.

    Admin sees all; everyone else sees every project they are a member of (any
    role) — so collaborators see shared projects too. ``is_owner``
    reflects the CALLER's project role, not the ``owner_id`` marker (an admin
    viewing a project they are not a member of gets ``is_owner=False``).
    """
    if user.role is UserRole.ADMIN:
        projects = form_project_repo.list_all(db)
    else:
        projects = form_project_repo.list_for_member(db, user.id)

    project_ids = [p.id for p in projects]
    counts = form_project_repo.variant_counts(db, project_ids)
    roles = form_project_member_repo.roles_for_user(db, user.id, project_ids)

    # Avatar-stack data in ONE batched query (no per-row members fetch / N+1).
    # Rows arrive grouped by project, owners-first: take the first ≤3 for the
    # preview and count the rest per project.
    previews: dict[uuid.UUID, list[FormProjectMemberRead]] = {}
    member_count: dict[uuid.UUID, int] = {}
    for member, member_user in form_project_member_repo.list_for_projects(
        db, project_ids
    ):
        member_count[member.project_id] = member_count.get(member.project_id, 0) + 1
        bucket = previews.setdefault(member.project_id, [])
        if len(bucket) < 3:
            bucket.append(_member_read(member, member_user))

    return [
        FormProjectListItem(
            id=p.id,
            type=p.type,
            name=p.name,
            description=p.description,
            event_date=p.event_date,
            status=p.status,
            owner_id=p.owner_id,
            is_owner=(roles.get(p.id) is ProjectRole.OWNER),
            variant_count=counts.get(p.id, 0),
            members_preview=previews.get(p.id, []),
            member_count=member_count.get(p.id, 0),
            created_at=p.created_at,
            updated_at=p.updated_at,
        )
        for p in projects
    ]


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


def get_project(db: Session, project_id: uuid.UUID, user: User) -> FormProject:
    return require_project_member(db, project_id, user)


# ---------------------------------------------------------------------------
# Scan readiness (identification; enforced on ingestion)
# ---------------------------------------------------------------------------


def get_scan_readiness(
    db: Session, project_id: uuid.UUID, user: User
) -> ScanReadiness:
    """Whether *project_id*'s PUBLISHED variants are coherently configured for a
    scan batch — the read-only pre-check behind the FE's banner.

    Never raises for a "not ready" state — that is a payload value, not an HTTP
    error. Only ``require_project_member`` may raise (404 unknown project, 403
    non-member); any member may run the check. The rule itself is
    :func:`compute_scan_readiness`, which batch creation and every upload also
    enforce, so the banner and the refusal cannot disagree.
    """
    require_project_member(db, project_id, user)
    return compute_scan_readiness(db, project_id)


def compute_scan_readiness(db: Session, project_id: uuid.UUID) -> ScanReadiness:
    """The scan-readiness rule for *project_id*, with no membership check (the
    callers have already done theirs). The rule:
      * 0 published variants → not ready (nothing to scan);
      * exactly 1            → ready, no constraint (single variant, no matching
                               ambiguity);
      * >1                   → ready only if one PUBLISHED variant carries a
                               VARIANT_IDENTIFIER binding, whose id is returned
                               so the FE can point at it.

    Checked FIRST, ahead of that rule: two PUBLISHED variants sharing the same
    VARIANT_IDENTIFIER value. It is the most severe misconfiguration on this path
    — an identifier IS present, so nothing looks wrong, yet matching cannot tell
    the variants apart and would attribute roughly half the submissions to the
    wrong one. ``publish_variant`` refuses to create such a collision, so this
    only surfaces data stored before that gate existed (defence in depth).
    """
    published_ids = form_variant_repo.list_published_variant_ids(db, project_id)
    count = len(published_ids)

    collisions = code_object_repo.find_duplicate_variant_identifier_values(
        db, project_id
    )
    colliding_variant_ids = sorted(
        {vid for match in collisions for vid in match.candidate_ids}
    )
    if colliding_variant_ids:
        return ScanReadiness(
            ready=False,
            reason=(
                "Multiple published variants share the same "
                "VARIANT_IDENTIFIER value; scanning cannot reliably "
                "match sheets to a variant."
            ),
            published_variant_count=count,
            colliding_variant_ids=colliding_variant_ids,
        )

    if count == 0:
        return ScanReadiness(
            ready=False,
            reason="No published variant exists for this project.",
            published_variant_count=0,
        )
    if count == 1:
        return ScanReadiness(ready=True, published_variant_count=1)

    holder = form_variant_role_binding_repo.find_variant_with_role_among(
        db, published_ids, PageObjectRole.VARIANT_IDENTIFIER
    )
    if holder is not None:
        return ScanReadiness(
            ready=True,
            published_variant_count=count,
            variant_identifier_variant_id=holder,
        )
    return ScanReadiness(
        ready=False,
        reason=(
            "Multiple published variants exist but none has a "
            "VARIANT_IDENTIFIER role assigned; scanning cannot "
            "automatically match sheets to a variant."
        ),
        published_variant_count=count,
    )


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


def update_project(
    db: Session, project_id: uuid.UUID, data: FormProjectUpdate, user: User
) -> FormProject:
    # DECISION: collaborators MAY edit project name/description/event_date (all
    # content work is shared). To make this owner-only, swap to
    # require_project_owner — a one-line change.
    # NOTE: status is NOT settable here — it was removed from FormProjectUpdate.
    # Lifecycle changes go ONLY through transition_project (owner-only, graph-
    # validated), so this setattr loop can never touch project.status.
    project = require_project_member(db, project_id, user)

    changes = data.model_dump(exclude_unset=True)
    if not changes:
        return project

    for field, value in changes.items():
        setattr(project, field, value)
    db.flush()

    # Stringify enum values for a JSON-serialisable audit payload.
    audit_meta = {
        k: (v.value if hasattr(v, "value") else v) for k, v in changes.items()
    }
    audit_service.log_form_project_updated(db, user.id, project_id, audit_meta)
    db.commit()
    db.refresh(project)
    return project


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def delete_project(db: Session, project_id: uuid.UUID, user: User) -> None:
    """Delete a project and everything it owns — only when it is safe to do so.

    Gate: the project is deletable only when EVERY variant is still in DRAFT. If
    even one variant is PUBLISHED/ARCHIVED the delete is refused with 409 and
    nothing is removed (deleting it would cascade away published content).

    When all variants are DRAFT the ORM cascade removes the whole subtree
    atomically: variants → pages → PageObjects → typed configs
    (text/code/free_text/question+options/matrix+rows/cols/cells), plus any
    submission_batches, then the project itself. The operation is all-or-nothing
    (single transaction); no orphan rows survive.
    """
    project = require_project_owner(db, project_id, user)

    non_draft_count = form_project_repo.count_non_draft_variants(db, project_id)
    if non_draft_count > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Project can be deleted only when all its variants are DRAFT.",
        )

    # Collect IMAGE file paths before the cascade removes the config rows — the
    # ORM deletes the rows but not the on-disk binaries.
    image_files = image_object_repo.list_file_paths_for_project(db, project_id)

    # Audit BEFORE the delete cascade so the project row still exists for the
    # entity reference; the log row commits in the same transaction.
    audit_service.log_form_project_deleted(db, user.id, project_id)
    form_project_repo.delete(db, project)
    db.commit()
    # Unlink the orphaned image files only after the DB delete is committed.
    for path in image_files:
        storage_service.delete_object_image(path)


# ---------------------------------------------------------------------------
# Members (read-only)
# ---------------------------------------------------------------------------


def list_members(
    db: Session, project_id: uuid.UUID, user: User
) -> list[FormProjectMemberRead]:
    """Roster of a project's members (owners first), for any member or admin.

    Authorizes via ``require_project_member`` (collaborators may view the
    roster), then maps each (member, user) row to the read schema. ``has_avatar``
    comes from the user's avatar column.
    """
    require_project_member(db, project_id, user)
    rows = form_project_member_repo.list_for_project(db, project_id)
    return [_member_read(member, member_user) for member, member_user in rows]


# ---------------------------------------------------------------------------
# Member management (owner-only)
#
# Every entry point gates on require_project_owner FIRST (admin bypasses). The
# invariants enforced server-side, regardless of FE gating:
#   * ≥1 OWNER always (cannot remove/demote the last owner → 409);
#   * never act on YOURSELF for owner removal/demotion (→ 400). "yourself" means
#     user_id == actor.id; an admin who is NOT a member may still manage, and the
#     self-rules apply only when the admin IS the target;
#   * no duplicate members (→ 409); only existing users (→ 404).
# All run in a single transaction with an audit row.
# ---------------------------------------------------------------------------


def add_collaborator(
    db: Session, project_id: uuid.UUID, user_id: uuid.UUID, actor: User
) -> FormProjectMemberRead:
    """Add an existing user to *project_id* as a COLLABORATOR (owner-only)."""
    project = require_project_owner(db, project_id, actor)
    assert_members_manageable(project)  # 409 in ARCHIVED

    target = user_repo.get_by_id(db, user_id)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )
    if form_project_member_repo.get(db, project_id, user_id) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Already a member"
        )

    member = form_project_member_repo.add(
        db,
        FormProjectMember(
            project_id=project_id, user_id=user_id, role=ProjectRole.COLLABORATOR
        ),
    )
    audit_service.log_event(
        db,
        actor_user_id=actor.id,
        action=audit_service.FORM_PROJECT_MEMBER_ADDED,
        entity_type="FormProjectMember",
        entity_id=member.id,
        metadata={"project_id": str(project_id), "user_id": str(user_id),
                  "role": ProjectRole.COLLABORATOR.value},
    )
    db.commit()
    return _member_read(member, target)


def remove_member(
    db: Session, project_id: uuid.UUID, user_id: uuid.UUID, actor: User
) -> None:
    """Remove a member from *project_id* (owner-only).

    An OWNER may not remove themselves, nor may the last owner be removed.
    """
    project = require_project_owner(db, project_id, actor)
    assert_members_manageable(project)  # 409 in ARCHIVED

    member = form_project_member_repo.get(db, project_id, user_id)
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Member not found"
        )

    if member.role is ProjectRole.OWNER:
        if user_id == actor.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You cannot remove yourself",
            )
        if form_project_member_repo.count_owners(db, project_id) <= 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A project must keep at least one owner",
            )

    # Capture before delete — the instance's attributes must not be read back
    # after the row is gone.
    removed_role = member.role.value
    member_id = member.id
    form_project_member_repo.delete(db, member)
    audit_service.log_event(
        db,
        actor_user_id=actor.id,
        action=audit_service.FORM_PROJECT_MEMBER_REMOVED,
        entity_type="FormProjectMember",
        entity_id=member_id,
        metadata={"project_id": str(project_id), "user_id": str(user_id),
                  "role": removed_role},
    )
    db.commit()


def set_member_role(
    db: Session,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    role: ProjectRole,
    actor: User,
) -> FormProjectMemberRead:
    """Promote/demote a member (owner-only).

    Promote COLLABORATOR→OWNER is always allowed. Demote OWNER→COLLABORATOR is
    forbidden on yourself (400) and on the last owner (409). A no-op (role
    unchanged) returns the current row.
    """
    project = require_project_owner(db, project_id, actor)
    assert_members_manageable(project)  # 409 in ARCHIVED

    member = form_project_member_repo.get(db, project_id, user_id)
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Member not found"
        )

    target = user_repo.get_by_id(db, user_id)  # FK guarantees presence

    if member.role is role:
        return _member_read(member, target)

    # The only state-changing case besides promotion is demotion OWNER→COLLAB.
    if member.role is ProjectRole.OWNER and role is ProjectRole.COLLABORATOR:
        if user_id == actor.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You cannot demote yourself",
            )
        if form_project_member_repo.count_owners(db, project_id) <= 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A project must keep at least one owner",
            )

    member.role = role
    db.flush()
    audit_service.log_event(
        db,
        actor_user_id=actor.id,
        action=audit_service.FORM_PROJECT_MEMBER_ROLE_CHANGED,
        entity_type="FormProjectMember",
        entity_id=member.id,
        metadata={"project_id": str(project_id), "user_id": str(user_id),
                  "role": role.value},
    )
    db.commit()
    return _member_read(member, target)
