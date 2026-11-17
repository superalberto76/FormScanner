# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""FormVariant lifecycle service — list, create, detail, update, publish, clone.

Authorization: access to a variant is governed by membership of its
parent FormProject — any member (OWNER or COLLABORATOR) or an admin. All variant
authoring/export is MEMBER-level.

On creation (and clone), blank A4 authoring page(s) are auto-provisioned via
form_variant_page_repo. Clone additionally deep-copies every PageObject (with
its typed config + children) from the source onto the mirrored pages.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.enums import FormVariantStatus, PageObjectRole
from app.models.form_variant import FormVariant
from app.models.form_variant_page import FormVariantPage
from app.models.user import User
from app.repositories import (
    code_object_repo,
    form_variant_page_repo,
    form_variant_repo,
    form_variant_role_binding_repo,
    image_object_repo,
    page_object_repo,
)
from app.schemas.form_variant import (
    FormVariantCreate,
    FormVariantDetail,
    FormVariantListItem,
    FormVariantPageRead,
    FormVariantUpdate,
)
from app.services import (
    audit_service,
    page_object_service,
    storage_service,
    submission_lock_service,
)
from app.services.form_project_service import (
    assert_can_publish,
    assert_project_editable,
    assert_variant_deletable,
    require_project_member,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_variant_with_access(
    db: Session, variant_id: uuid.UUID, user: User
) -> FormVariant:
    """Fetch a variant the user may access, or raise 404/403.

    Access is delegated to the parent project's membership check.
    """
    variant = form_variant_repo.get(db, variant_id)
    if variant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Form variant not found"
        )
    # Reuses the project-level member/admin gate (raises 404/403 as appropriate).
    require_project_member(db, variant.form_project_id, user)
    return variant


def _default_root_key(version_number: int) -> str:
    return f"variant-{version_number}"


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


def list_variants(
    db: Session, project_id: uuid.UUID, user: User
) -> list[FormVariantListItem]:
    # Authorize via parent project (404 if missing, 403 if not member/admin).
    require_project_member(db, project_id, user)

    variants = form_variant_repo.list_for_project(db, project_id)
    page_counts = form_variant_repo.page_counts(db, [v.id for v in variants])
    return [
        FormVariantListItem(
            id=v.id,
            form_project_id=v.form_project_id,
            root_key=v.root_key,
            name=v.name,
            description=v.description,
            version_number=v.version_number,
            status=v.status,
            published_at=v.published_at,
            page_count=page_counts.get(v.id, 0),
            created_at=v.created_at,
            updated_at=v.updated_at,
        )
        for v in variants
    ]


# ---------------------------------------------------------------------------
# Create (auto-provisions one blank first page)
# ---------------------------------------------------------------------------


def create_variant(
    db: Session, project_id: uuid.UUID, data: FormVariantCreate, user: User
) -> FormVariant:
    project = require_project_member(db, project_id, user)
    assert_project_editable(project)  # project must be DRAFT to author content

    version_number = form_variant_repo.get_next_version_number(db, project_id)
    root_key = data.root_key or _default_root_key(version_number)

    variant = FormVariant(
        form_project_id=project_id,
        root_key=root_key,
        name=data.name,
        description=data.description,
        status=FormVariantStatus.DRAFT,
        version_number=version_number,
        created_by_user_id=user.id,
    )
    form_variant_repo.add(db, variant)

    # Auto-provision the first blank A4 page (page_index = 1).
    form_variant_page_repo.add_blank_page(db, variant.id)

    audit_service.log_form_variant_created(
        db,
        user.id,
        variant.id,
        {"version_number": version_number, "root_key": root_key},
    )
    db.commit()
    db.refresh(variant)
    return variant


# ---------------------------------------------------------------------------
# Detail (with pages)
# ---------------------------------------------------------------------------


def get_variant_detail(
    db: Session, variant_id: uuid.UUID, user: User
) -> FormVariantDetail:
    variant = _get_variant_with_access(db, variant_id, user)
    pages = form_variant_page_repo.list_for_variant(db, variant_id)
    detail = FormVariantDetail.model_validate(variant)
    detail.pages = [FormVariantPageRead.model_validate(p) for p in pages]
    # Always populated at runtime (the schema default is None only so
    # model_validate(variant) doesn't fail). The FE reads this to freeze the
    # workspace when the project is no longer DRAFT — no extra round-trip.
    detail.form_project_status = variant.form_project.status
    return detail


# ---------------------------------------------------------------------------
# Update (DRAFT only, name/description)
# ---------------------------------------------------------------------------


def update_variant(
    db: Session, variant_id: uuid.UUID, data: FormVariantUpdate, user: User
) -> FormVariant:
    variant = _get_variant_with_access(db, variant_id, user)
    assert_project_editable(variant.form_project)  # project must be DRAFT too

    if variant.status is not FormVariantStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cannot update a {variant.status.value} variant; "
                "clone it to make changes."
            ),
        )

    changes = data.model_dump(exclude_unset=True)
    if not changes:
        return variant

    for field, value in changes.items():
        setattr(variant, field, value)
    variant.updated_by_user_id = user.id
    db.flush()

    audit_service.log_form_variant_updated(db, user.id, variant_id, changes)
    db.commit()
    db.refresh(variant)
    return variant


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------


def _assert_no_objects_in_reserved_bands(db: Session, variant_id: uuid.UUID) -> None:
    """409 ``OBJECTS_IN_RESERVED_BANDS`` listing every object of the variant
    that sits, even partly, in a reserved band or off its page. Hidden objects
    count too: they return visible with a click.
    The system never moves them: the author does, then publishes again."""
    offending = [
        o
        for o in page_object_repo.list_for_variant_with_page(db, variant_id)
        if page_object_service.in_reserved_band(o, o.form_variant_page)
    ]
    if not offending:
        return
    objects = [
        {
            "id": str(o.id),
            "label": o.label,
            "object_type": o.object_type.value,
            "page_index": o.form_variant_page.page_index,
        }
        for o in offending
    ]
    names = ", ".join(
        f"{o['label'] or o['object_type']} (page {o['page_index']})" for o in objects
    )
    count = len(objects)
    noun = "object is" if count == 1 else "objects are"
    move = "Move it" if count == 1 else "Move them"
    raise submission_lock_service.structured_conflict(
        page_object_service.OBJECTS_IN_RESERVED_BANDS,
        f"Cannot publish: {count} {noun} in the reserved bands: {names}. "
        f"{move} inside the page area.",
        objects=objects,
    )


def publish_variant(
    db: Session, variant_id: uuid.UUID, user: User
) -> FormVariant:
    """Publish a DRAFT variant.

    Gates: the variant must have at least one page, and no object (hidden ones
    included) may sit in the reserved bands along the sides of its pages
    (409 ``OBJECTS_IN_RESERVED_BANDS`` with the list).

    One content gate on top of that: if the variant carries a
    VARIANT_IDENTIFIER, its value must not already be published by a SIBLING
    variant of the same project. Two published variants sharing an identifier
    value are indistinguishable on paper — matching resolves them
    deterministically, so roughly half the submissions would be attributed to
    the wrong variant with no error anywhere. Blocking at publish time is the
    only moment the author can still fix it; DRAFT siblings are irrelevant (they
    are never scanned) and a variant with no identifier cannot collide at all.
    """
    variant = _get_variant_with_access(db, variant_id, user)
    assert_can_publish(variant.form_project)  # project must be DRAFT or ACTIVE

    if variant.status is not FormVariantStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot publish a {variant.status.value} variant.",
        )

    page_count = form_variant_repo.count_pages(db, variant_id)
    if page_count == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot publish: variant has no pages.",
        )

    _assert_no_objects_in_reserved_bands(db, variant_id)

    # VARIANT_IDENTIFIER uniqueness among the project's PUBLISHED variants. The
    # role is optional: without a binding there is no identifier and
    # therefore no possible collision, so the check is skipped entirely.
    binding = form_variant_role_binding_repo.get_by_variant_and_role(
        db, variant_id, PageObjectRole.VARIANT_IDENTIFIER
    )
    if binding is not None:
        value = binding.page_object.code_config.value
        # This variant is still DRAFT here, so it can never be its own match.
        match = code_object_repo.find_published_variant_by_identifier_value(
            db, variant.form_project_id, value
        )
        if match is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Cannot publish: VARIANT_IDENTIFIER value {value!r} "
                    "collides with already-published variant(s): "
                    + ", ".join(str(vid) for vid in match.candidate_ids)
                ),
            )

    variant.status = FormVariantStatus.PUBLISHED
    variant.published_at = datetime.now(timezone.utc)
    variant.updated_by_user_id = user.id
    db.flush()

    audit_service.log_form_variant_published(db, user.id, variant_id)
    db.commit()
    db.refresh(variant)
    return variant


# ---------------------------------------------------------------------------
# Clone (copies metadata, mirrors pages, deep-copies all PageObjects)
# ---------------------------------------------------------------------------


def clone_variant(
    db: Session, variant_id: uuid.UUID, user: User
) -> FormVariant:
    source = _get_variant_with_access(db, variant_id, user)
    # Clone == "create variant" (DRAFT-only, confirmed by product). The source's
    # project is also the clone's target project, so gate on it.
    assert_project_editable(source.form_project)

    next_num = form_variant_repo.get_next_version_number(
        db, source.form_project_id
    )
    new_variant = FormVariant(
        form_project_id=source.form_project_id,
        root_key=source.root_key,
        name=f"{source.name} (copy)",
        description=source.description,
        status=FormVariantStatus.DRAFT,
        version_number=next_num,
        # original_file_path NOT copied: a clone needs its own source.
        created_by_user_id=user.id,
    )
    form_variant_repo.add(db, new_variant)

    # Mirror the source's pages, deep-copying every PageObject (with its typed
    # config + children) onto each new page so the clone is fully independent.
    source_pages = form_variant_page_repo.list_for_variant(db, source.id)
    objects_copied = 0
    if source_pages:
        # One shared map for the whole clone so a group spanning several pages is
        # rewritten to the SAME new id everywhere — keeping groups internal to the
        # cloned variant and never leaking the source's group ids.
        group_id_map: dict[uuid.UUID, uuid.UUID] = {}
        for page in source_pages:
            new_page = form_variant_page_repo.add_blank_page(
                db, new_variant.id, page_index=page.page_index
            )
            objects_copied += page_object_service.clone_objects_to_page(
                db, page.id, new_page.id, group_id_map=group_id_map
            )
    else:
        # Defensive: ensure a cloned variant always has at least one page.
        form_variant_page_repo.add_blank_page(db, new_variant.id)

    audit_service.log_form_variant_cloned(
        db,
        user.id,
        new_variant.id,
        {
            "from_variant_id": str(source.id),
            "new_version_number": next_num,
            "pages_mirrored": len(source_pages) or 1,
            "objects_copied": objects_copied,
        },
    )
    db.commit()
    db.refresh(new_variant)
    return new_variant


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def list_pages(
    db: Session, variant_id: uuid.UUID, user: User
) -> list[FormVariantPageRead]:
    _get_variant_with_access(db, variant_id, user)
    pages = form_variant_page_repo.list_for_variant(db, variant_id)
    return [FormVariantPageRead.model_validate(p) for p in pages]


def add_page(
    db: Session, variant_id: uuid.UUID, user: User
) -> FormVariantPage:
    """Append a new blank A4 page to a DRAFT variant.

    Read-only variants (PUBLISHED/ARCHIVED) cannot receive new pages. The
    page_index is computed server-side as (max existing index + 1), and the
    page is provisioned via the same blank-A4 helper used by create/clone (DRY).
    """
    variant = _get_variant_with_access(db, variant_id, user)
    assert_project_editable(variant.form_project)  # project must be DRAFT too

    if variant.status is not FormVariantStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot add a page to a {variant.status.value} variant.",
        )

    page_index = form_variant_page_repo.next_page_index(db, variant_id)
    page = form_variant_page_repo.add_blank_page(
        db, variant_id, page_index=page_index
    )

    # Lazy replication: every linked shared group copies its template
    # onto the new page as a linked member, in this same transaction.
    replicated = page_object_service.replicate_linked_groups_to_page(
        db, variant_id, page.id
    )

    audit_service.log_form_variant_page_added(
        db,
        user.id,
        variant_id,
        {
            "page_id": str(page.id),
            "page_index": page_index,
            "linked_objects_replicated": replicated,
        },
    )
    db.commit()
    db.refresh(page)
    return page


def duplicate_page(
    db: Session,
    variant_id: uuid.UUID,
    page_id: uuid.UUID,
    user: User,
) -> FormVariantPage:
    """Duplicate one page of a DRAFT variant.

    Creates a NEW page whose content REPLICATES the source page, places it
    immediately AFTER the source, then renumbers pages to a contiguous 1..n.

    Identical to the source: NO lazy "show on all pages" replication is
    triggered — we copy the source page's objects ourselves, so a group that was
    not present on the source page does NOT appear on the duplicate. Per object
    (page_object_service.duplicate_objects_to_page): a live linked member joins the
    SAME group as a new linked member (live sync preserved); an ordinary or
    detached object becomes an independent page-local copy. IMAGE binaries are
    duplicated per page via the reused clone path. DRAFT only — mirrors add_page's
    gates verbatim so the two stay consistent.
    """
    variant = _get_variant_with_access(db, variant_id, user)
    assert_project_editable(variant.form_project)  # project must be DRAFT too

    if variant.status is not FormVariantStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot duplicate a page of a {variant.status.value} variant.",
        )

    pages = form_variant_page_repo.list_for_variant(db, variant_id)
    source = next((p for p in pages if p.id == page_id), None)
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Form variant page not found",
        )

    # Create the new page at the end first (NO lazy replication — add_blank_page,
    # not add_page — so we copy the source's objects ourselves and the duplicate
    # is identical to the source).
    new_page = form_variant_page_repo.add_blank_page(
        db,
        variant_id,
        page_index=form_variant_page_repo.next_page_index(db, variant_id),
    )
    objects_copied = page_object_service.duplicate_objects_to_page(
        db, source.id, new_page.id
    )

    # Reorder so the new page sits immediately after the source, contiguous 1..n.
    ordered = [p.id for p in pages]  # current order (source somewhere in it)
    src_pos = ordered.index(source.id)
    ordered.insert(src_pos + 1, new_page.id)  # new page right after the source
    form_variant_page_repo.set_page_order(db, variant_id, ordered)

    audit_service.log_form_variant_page_duplicated(
        db,
        user.id,
        variant_id,
        {
            "source_page_id": str(source.id),
            "page_id": str(new_page.id),
            "objects_copied": objects_copied,
        },
    )
    db.commit()
    db.refresh(new_page)  # page_index now reflects the post-reorder position
    return new_page


def reorder_pages(
    db: Session,
    variant_id: uuid.UUID,
    ordered_page_ids: list[uuid.UUID],
    user: User,
) -> list[FormVariantPageRead]:
    """Persist a new page order for a DRAFT variant and renumber 1..n.

    ``ordered_page_ids`` must be a full permutation of the variant's current
    pages. Read-only variants (PUBLISHED/ARCHIVED) reject the operation (409).
    The whole renumbering rides one transaction.
    """
    variant = _get_variant_with_access(db, variant_id, user)
    assert_project_editable(variant.form_project)  # project must be DRAFT too

    if variant.status is not FormVariantStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot reorder pages of a {variant.status.value} variant.",
        )

    pages = form_variant_page_repo.list_for_variant(db, variant_id)
    current_ids = {p.id for p in pages}
    if len(ordered_page_ids) != len(pages) or set(ordered_page_ids) != current_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ordered_page_ids must be a permutation of the variant pages",
        )

    form_variant_page_repo.set_page_order(db, variant_id, ordered_page_ids)

    audit_service.log_form_variant_page_reordered(
        db,
        user.id,
        variant_id,
        {"order": [str(pid) for pid in ordered_page_ids]},
    )
    db.commit()

    reordered = form_variant_page_repo.list_for_variant(db, variant_id)
    return [FormVariantPageRead.model_validate(p) for p in reordered]


def delete_page(
    db: Session,
    variant_id: uuid.UUID,
    page_id: uuid.UUID,
    user: User,
) -> list[FormVariantPageRead]:
    """Remove one page of a DRAFT variant, scoped to that page only.

    The delete NEVER goes through page_object_service.delete_object (which is
    group-aware and would wipe a whole linked group across pages). It removes
    ONLY this page's PageObject rows via the ORM cascade
    (FormVariantPage.page_objects = all, delete-orphan), so:
      - ordinary objects and detached copies on the page are deleted (this
        instance only);
      - a linked member on the page loses only this page's copy; its siblings on
        other pages stay linked and intact (the group template is dynamic — the
        lowest-page_index member — so no explicit promotion is needed);
      - a group whose only member sat on this page simply ceases to exist.
    Remaining pages are renumbered 1..n. Read-only variants reject (409); the
    last remaining page cannot be removed (409). On-disk IMAGE binaries are
    unlinked AFTER the commit (best-effort), mirroring delete_object.
    """
    variant = _get_variant_with_access(db, variant_id, user)
    assert_project_editable(variant.form_project)  # project must be DRAFT too

    if variant.status is not FormVariantStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot remove a page from a {variant.status.value} variant.",
        )

    pages = form_variant_page_repo.list_for_variant(db, variant_id)
    page = next((p for p in pages if p.id == page_id), None)
    if page is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Form variant page not found",
        )
    if len(pages) <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot remove the only page of a variant.",
        )

    removed_index = page.page_index
    objects = page_object_repo.list_for_page(db, page_id)
    objects_removed = len(objects)
    linked_members_removed = sum(
        1 for o in objects if o.shared_across_pages and o.shared_group_id is not None
    )
    # Capture IMAGE binaries BEFORE the cascade drops their config rows (the ORM
    # removes the rows but not the on-disk files).
    image_files = image_object_repo.list_file_paths_for_page(db, page_id)

    # SCOPED delete: cascade removes only this page's objects (+ typed configs).
    form_variant_page_repo.delete(db, page)

    # Renumber the survivors (already in page_index order) to a contiguous 1..n.
    remaining_ids = [p.id for p in pages if p.id != page_id]
    form_variant_page_repo.set_page_order(db, variant_id, remaining_ids)

    audit_service.log_form_variant_page_removed(
        db,
        user.id,
        variant_id,
        {
            "page_id": str(page_id),
            "page_index": removed_index,
            "objects_removed": objects_removed,
            "linked_members_removed": linked_members_removed,
        },
    )
    db.commit()

    # Unlink orphaned image files only after the DB delete is committed, so a
    # rollback never leaves a committed row pointing at a deleted file.
    for path in image_files:
        storage_service.delete_object_image(path)

    remaining = form_variant_page_repo.list_for_variant(db, variant_id)
    return [FormVariantPageRead.model_validate(p) for p in remaining]


# ---------------------------------------------------------------------------
# Delete (DRAFT only — cascades pages + PageObjects + typed configs)
# ---------------------------------------------------------------------------


def delete_variant(db: Session, variant_id: uuid.UUID, user: User) -> None:
    """Delete a DRAFT variant and all of its authored content.

    Gate: only DRAFT variants are deletable; PUBLISHED/ARCHIVED are refused with
    409 (they may already be in use for printing/scanning). When DRAFT, the ORM
    cascade removes the whole subtree atomically — pages → PageObjects → typed
    configs (text/code/free_text/question+options/matrix+rows/cols/cells) — so
    no orphan rows are left behind.
    """
    variant = _get_variant_with_access(db, variant_id, user)
    # Housekeeping: deleting a DRAFT variant is allowed in DRAFT/ACTIVE/CLOSED,
    # blocked only when the project is ARCHIVED.
    assert_variant_deletable(variant.form_project)

    if variant.status is not FormVariantStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only DRAFT variants can be deleted.",
        )

    # Collect IMAGE file paths before the cascade removes the config rows — the
    # ORM deletes the rows but not the on-disk binaries.
    image_files = image_object_repo.list_file_paths_for_variant(db, variant_id)

    # Audit BEFORE the delete cascade so the variant row still exists for the
    # entity reference; the log row is committed in the same transaction.
    audit_service.log_form_variant_deleted(
        db,
        user.id,
        variant_id,
        {"version_number": variant.version_number},
    )
    form_variant_repo.delete(db, variant)
    db.commit()
    # Unlink the orphaned image files only after the DB delete is committed.
    for path in image_files:
        storage_service.delete_object_image(path)
