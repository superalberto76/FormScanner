# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""PageObject service.

Owns:
  - shared access/authorization helpers reused by all typed object services
  - generic list / get / update (geometry) / delete
  - the typed-read builder that turns a PageObject ORM row into the right
    Pydantic read model

Authorization mirrors form_variant_service: access to an object is governed by
access to its parent FormVariant's FormProject (owner or admin). All MUTATIONS
additionally require the parent FormVariant to be DRAFT (else 409).
"""
from __future__ import annotations

import uuid
from pathlib import PurePath
from types import SimpleNamespace

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.code_object_config import CodeObjectConfig
from app.models.enums import FormVariantStatus, PageObjectRole, PageObjectType
from app.models.form_variant import FormVariant
from app.models.form_variant_page import FormVariantPage
from app.models.free_text_area_config import FreeTextAreaConfig
from app.models.image_object_config import ImageObjectConfig
from app.models.matrix_cell_def import MatrixCellDef
from app.models.matrix_col_def import MatrixColDef
from app.models.matrix_object_config import MatrixObjectConfig
from app.models.matrix_row_def import MatrixRowDef
from app.models.page_object import PageObject
from app.models.question_object_config import QuestionObjectConfig
from app.models.question_option import QuestionOption
from app.models.text_object_config import TextObjectConfig
from app.models.user import User
from app.repositories import (
    code_object_repo,
    form_variant_page_repo,
    form_variant_repo,
    form_variant_role_binding_repo,
    free_text_area_repo,
    image_object_repo,
    matrix_object_repo,
    page_object_repo,
    question_object_repo,
    text_object_repo,
)
from app.schemas.matrix_object import MatrixPageObjectRead
from app.schemas.page_object import (
    CodePageObjectRead,
    FreeTextAreaPageObjectRead,
    ImagePageObjectRead,
    PageObjectUpdate,
    PageObjectWithConfig,
    TextPageObjectRead,
)
from app.schemas.question_object import QuestionPageObjectRead
from app.services import audit_service, review_rules, storage_service
from app.services.form_project_service import (
    assert_project_editable,
    require_project_member,
)
from app.services.submission_lock_service import structured_conflict
from app.utils import alignment_marks

# Structured 409 codes of the reserved bands.
OBJECT_IN_RESERVED_BAND = "OBJECT_IN_RESERVED_BAND"
OBJECTS_IN_RESERVED_BANDS = "OBJECTS_IN_RESERVED_BANDS"

# The PATCH fields that change an object's rectangle.
_GEOMETRY_FIELDS = frozenset({"x", "y", "width", "height"})

# Maps object_type -> the typed read model used for that variant.
_READ_BY_TYPE = {
    PageObjectType.TEXT: TextPageObjectRead,
    PageObjectType.CODE: CodePageObjectRead,
    PageObjectType.FREE_TEXT_AREA: FreeTextAreaPageObjectRead,
    PageObjectType.QUESTION: QuestionPageObjectRead,
    PageObjectType.MATRIX: MatrixPageObjectRead,
    PageObjectType.IMAGE: ImagePageObjectRead,
}

# Base fields (geometry + presentation) of a PageObjectUpdate that propagate to
# the other linked members of a shared group. Typed config is NOT here — it
# propagates through the typed-config resync below. This set is exactly the
# PageObjectUpdate surface today; spelling it out keeps propagation correct if
# that schema grows.
_PROPAGATED_BASE_FIELDS = frozenset(
    {
        "x",
        "y",
        "width",
        "height",
        "z_index",
        "label",
        "description",
        "visible",
        "locked",
    }
)


# ---------------------------------------------------------------------------
# Row labels of a respondent-ID grid
#
# Here rather than in page_object_role_service, which imports this module: the
# re-link paths below need the rule too.
# ---------------------------------------------------------------------------


def invalid_identifier_row_label(label: str) -> bool:
    """Whether *label* cannot name a row of a respondent-ID grid.

    The marked row's label is the character the reading writes for its column
    (``respondent_identifier_extraction_service``): a blank one drops a digit
    unnoticed, and a "?" is the character reserved for an empty column, which
    the reading would then report as incomplete without forcing review. Only a
    MATRIX holding RESPONDENT_IDENTIFIER (a linked copy included) is bound by
    this; any other matrix keeps free labels.
    """
    return not label.strip() or review_rules.UNREAD_DIGIT in label


def identifier_row_label_conflict(order_index: int) -> HTTPException:
    """The 409 for an invalid row label on a respondent-ID grid, shown as is by
    the FE editor's toast. Rows are numbered from 1, as the editor names
    them."""
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            "Row labels of a respondent identifier grid cannot be empty or "
            f"contain '{review_rules.UNREAD_DIGIT}' (row {order_index + 1})."
        ),
    )


def check_identifier_row_labels(matrix: PageObject) -> None:
    """409 on the first invalid row label of *matrix* (rows without a
    definition read as their index, never blank)."""
    if matrix.object_type is not PageObjectType.MATRIX or matrix.matrix_config is None:
        return
    for row in sorted(matrix.matrix_config.rows, key=lambda r: r.order_index):
        if invalid_identifier_row_label(row.label):
            raise identifier_row_label_conflict(row.order_index)


def _check_identifier_rows_on_relink(
    db: Session, members: list[PageObject], sources: list[PageObject]
) -> None:
    """Before re-linking a group: when one of *members* holds
    RESPONDENT_IDENTIFIER, every copy ends up carrying it, so the rows the
    linked copies will have — those of *sources* — must be valid. Checked
    before anything changes."""
    holds_identifier = any(
        (binding := form_variant_role_binding_repo.get_by_page_object(db, m.id))
        is not None
        and binding.role is PageObjectRole.RESPONDENT_IDENTIFIER
        for m in members
    )
    if holds_identifier:
        for source in sources:
            check_identifier_row_labels(source)


# ---------------------------------------------------------------------------
# Access helpers (reused by every typed object service)
# ---------------------------------------------------------------------------


def load_page_authorized(
    db: Session, page_id: uuid.UUID, user: User, *, require_draft: bool = False
) -> tuple[FormVariantPage, FormVariant]:
    """Resolve a page + its variant, enforcing member/admin access.

    When *require_draft* is True, also enforce that the variant is DRAFT (409).
    Raises 404 if the page or its variant does not exist.
    """
    page = form_variant_page_repo.get(db, page_id)
    if page is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Form variant page not found"
        )
    variant = form_variant_repo.get(db, page.form_variant_id)
    if variant is None:  # defensive — FK guarantees presence
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Form variant not found"
        )
    # Member/admin gate via the parent project (raises 404/403).
    project = require_project_member(db, variant.form_project_id, user)
    if require_draft:
        _assert_variant_draft(variant)
        # Combined gate: object authoring requires BOTH the variant AND the
        # project to be DRAFT. Every object mutation (create/update/delete/share/
        # unshare/relink/resync) funnels through require_draft=True, so this one
        # point blocks them all when the project is ACTIVE/CLOSED/ARCHIVED. Read
        # paths (require_draft=False) get no new gate.
        assert_project_editable(project)
    return page, variant


def load_object_authorized(
    db: Session, object_id: uuid.UUID, user: User, *, require_draft: bool = False
) -> tuple[PageObject, FormVariant]:
    """Resolve a PageObject + its variant, enforcing owner/admin access.

    When *require_draft* is True, also enforce that the variant is DRAFT (409).
    """
    obj = page_object_repo.get(db, object_id)
    if obj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Page object not found"
        )
    _, variant = load_page_authorized(
        db, obj.form_variant_page_id, user, require_draft=require_draft
    )
    return obj, variant


def _assert_variant_draft(variant: FormVariant) -> None:
    if variant.status is not FormVariantStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cannot modify objects: parent variant is {variant.status.value}. "
                "Only DRAFT variants are editable."
            ),
        )


def page_size_px(page: FormVariantPage) -> tuple[float, float]:
    """The page's size in px, A4 at 96 dpi when it has none (as the export's
    ``page_metrics`` does)."""
    return (
        page.width_px or form_variant_page_repo.A4_96DPI_WIDTH_PX,
        page.height_px or form_variant_page_repo.A4_96DPI_HEIGHT_PX,
    )


def in_reserved_band(obj, page: FormVariantPage) -> bool:
    """Whether *obj* (anything with ``x``, ``y``, ``width``, ``height``: a
    PageObject or a create payload) is not wholly inside the area of *page*
    outside the reserved bands."""
    return alignment_marks.in_reserved_band(
        obj.x, obj.y, obj.width, obj.height, *page_size_px(page)
    )


def assert_outside_bands(obj, page: FormVariantPage) -> None:
    """409 ``OBJECT_IN_RESERVED_BAND`` when *obj* would sit, even partly, in a
    reserved band of *page* or off the page. Called on every object create and
    on every PATCH that touches the geometry; copies made by the system (clone,
    page duplication, linked replicas) are not checked."""
    if in_reserved_band(obj, page):
        raise structured_conflict(
            OBJECT_IN_RESERVED_BAND,
            "Objects cannot be placed in the reserved bands along the edges "
            "of the page (15 mm).",
            page_index=page.page_index,
        )


def assert_object_type(obj: PageObject, expected: PageObjectType) -> None:
    """Guard a typed-config endpoint against a mismatched object_type (409)."""
    if obj.object_type is not expected:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Object {obj.id} is {obj.object_type.value}, "
                f"not {expected.value}; wrong typed-config endpoint."
            ),
        )


# ---------------------------------------------------------------------------
# Typed-read builder
# ---------------------------------------------------------------------------


def build_typed_read(obj: PageObject):
    """Return the correct typed Pydantic read model for *obj*.

    Relies on PageObject's relationships (text_config, code_config, …,
    question_config.options, matrix_config.rows/cols/cells) being loadable in
    the current session; Pydantic from_attributes traverses them.
    """
    model = _READ_BY_TYPE[obj.object_type]
    return model.model_validate(obj)


# ---------------------------------------------------------------------------
# List / get / update / delete
# ---------------------------------------------------------------------------


def list_objects(
    db: Session, page_id: uuid.UUID, user: User
) -> list[PageObjectWithConfig]:
    """Every object of a page, each enriched with its typed config.

    Uses the eager-loading repo query so the configs (+ their children) come back
    in one round-trip — the canvas renders content previews without a per-object
    fetch (no N+1).
    """
    load_page_authorized(db, page_id, user)
    objects = page_object_repo.list_for_page_with_configs(db, page_id)
    return [PageObjectWithConfig.model_validate(o) for o in objects]


def get_object(db: Session, object_id: uuid.UUID, user: User):
    obj, _ = load_object_authorized(db, object_id, user)
    return build_typed_read(obj)


def update_object(
    db: Session, object_id: uuid.UUID, data: PageObjectUpdate, user: User
):
    obj, _ = load_object_authorized(db, object_id, user, require_draft=True)

    changes = data.model_dump(exclude_unset=True)
    if changes.keys() & _GEOMETRY_FIELDS:
        _assert_new_geometry_outside_bands(db, obj, changes)
    if changes:
        for field, value in changes.items():
            setattr(obj, field, value)
        db.flush()
        propagated = _propagate_base_fields_to_group(db, obj, changes)
        metadata = dict(changes)
        if propagated:
            metadata["propagated_to_linked_members"] = propagated
        audit_service.log_event(
            db,
            actor_user_id=user.id,
            action=audit_service.PAGE_OBJECT_UPDATED,
            entity_type="PageObject",
            entity_id=obj.id,
            metadata=metadata,
        )
        db.commit()
    db.refresh(obj)
    return build_typed_read(obj)


def _assert_new_geometry_outside_bands(
    db: Session, obj: PageObject, changes: dict
) -> None:
    """Before writing a geometry PATCH: the resulting rectangle must lie
    outside the bands of *obj*'s page and of the page of every linked copy it
    propagates to. Only geometry changes are checked, so an object already in
    a band still takes label, z-index, visibility and lock edits; detached
    copies are independent and not checked."""
    rect = SimpleNamespace(
        **{
            field: changes[field] if changes.get(field) is not None else getattr(obj, field)
            for field in _GEOMETRY_FIELDS
        }
    )
    members = [obj]
    if obj.shared_across_pages and obj.shared_group_id is not None:
        members += [
            s
            for s in page_object_repo.list_by_shared_group(db, obj.shared_group_id)
            if s.id != obj.id and s.shared_across_pages
        ]
    for member in members:
        assert_outside_bands(rect, member.form_variant_page)


def _propagate_base_fields_to_group(
    db: Session, obj: PageObject, changes: dict
) -> int:
    """Mirror base-field edits of a linked member onto its linked siblings.

    Covers canvas drag/resize (x/y/w/h) and toolbar z-order (z_index) — both ride
    the same PATCH — plus the presentation fields. Detached members of the group
    (shared_across_pages=False) are deliberately left untouched. Writes the
    siblings directly (no recursive update_object call) to avoid re-propagation,
    and stays in the caller's transaction. Returns the number of siblings updated.
    """
    if not (obj.shared_across_pages and obj.shared_group_id is not None):
        return 0
    base_changes = {k: v for k, v in changes.items() if k in _PROPAGATED_BASE_FIELDS}
    if not base_changes:
        return 0

    updated = 0
    for sibling in page_object_repo.list_by_shared_group(db, obj.shared_group_id):
        if sibling.id == obj.id or not sibling.shared_across_pages:
            continue
        for field, value in base_changes.items():
            setattr(sibling, field, value)
        updated += 1
    if updated:
        db.flush()
    return updated


def delete_object(db: Session, object_id: uuid.UUID, user: User) -> None:
    obj, _ = load_object_authorized(db, object_id, user, require_draft=True)

    # Deleting a LINKED member removes the object from every page it is shared on:
    # the whole linked group goes. Detached members (shared_across_pages=False)
    # keep their group_id and are NOT touched. An ordinary object deletes just
    # itself.
    if obj.shared_across_pages and obj.shared_group_id is not None:
        targets = [
            m
            for m in page_object_repo.list_by_shared_group(db, obj.shared_group_id)
            if m.shared_across_pages
        ]
    else:
        targets = [obj]

    # Block the WHOLE deletion if any target still holds a special scanning role
    # — a role must be explicitly removed first. This is checked over every
    # target, so a single click that expands into a linked group is refused
    # when even one member of the group carries a role.
    role_blocked = []
    for target in targets:
        binding = form_variant_role_binding_repo.get_by_page_object(db, target.id)
        if binding is not None:
            role_blocked.append((target, binding))
    if role_blocked:
        listed = "; ".join(f"{t.id} ({b.role.value})" for t, b in role_blocked)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Cannot delete: the following object(s) still hold a special "
                f"role and must be freed first: {listed}."
            ),
        )

    # Capture IMAGE file paths (if any) BEFORE the rows are deleted; the ORM
    # cascade removes the config rows but not the on-disk binaries.
    image_files: list[str] = []
    deleted_ids: list[uuid.UUID] = []
    for target in targets:
        deleted_ids.append(target.id)
        if (
            target.object_type is PageObjectType.IMAGE
            and target.image_config is not None
            and target.image_config.file_path
        ):
            image_files.append(target.image_config.file_path)
        page_object_repo.delete(db, target)

    for deleted_id in deleted_ids:
        audit_service.log_event(
            db,
            actor_user_id=user.id,
            action=audit_service.PAGE_OBJECT_DELETED,
            entity_type="PageObject",
            entity_id=deleted_id,
        )
    db.commit()
    # Unlink only after the DB delete is committed, so a rollback never leaves a
    # committed row pointing at a deleted file.
    for image_file in image_files:
        storage_service.delete_object_image(image_file)


# ---------------------------------------------------------------------------
# Share / unshare ("show on all pages")
#
# Activating the flag does NOT copy the object onto existing pages: it only makes
# the object the template of its group. New pages then receive a linked copy via
# the lazy replication on add-page (replicate_linked_groups_to_page).
# ---------------------------------------------------------------------------


def share_object(db: Session, object_id: uuid.UUID, user: User):
    """Link an object so it shows on all pages (DRAFT only).

    Fresh activation (no group yet): mint a new group id and mark the object as a
    linked member — it becomes the group's template. If the object already has a
    group (e.g. it was unlinked earlier), re-link the WHOLE group without
    overwriting any config — the yes/no-with-overwrite choice is
    relink_object.
    """
    obj, _ = load_object_authorized(db, object_id, user, require_draft=True)

    if obj.shared_group_id is None:
        obj.shared_group_id = uuid.uuid4()
        obj.shared_across_pages = True
    else:
        members = page_object_repo.list_by_shared_group(db, obj.shared_group_id)
        # Nothing is overwritten: every copy keeps its own rows.
        _check_identifier_rows_on_relink(db, members, members)
        for member in members:
            member.shared_across_pages = True
    db.flush()

    audit_service.log_event(
        db,
        actor_user_id=user.id,
        action=audit_service.PAGE_OBJECT_SHARED,
        entity_type="PageObject",
        entity_id=obj.id,
        metadata={"shared_group_id": str(obj.shared_group_id)},
    )
    db.commit()
    db.refresh(obj)
    return build_typed_read(obj)


def unshare_object(db: Session, object_id: uuid.UUID, user: User):
    """Unlink: detach every member of the group (keep shared_group_id) — DRAFT only.

    Members stay on their pages as independent copies and new pages stop
    receiving copies. A no-op for an object that has no group.
    """
    obj, _ = load_object_authorized(db, object_id, user, require_draft=True)

    if obj.shared_group_id is not None:
        members = page_object_repo.list_by_shared_group(db, obj.shared_group_id)
        # Block the WHOLE unshare if any member still holds a special scanning
        # role: unsharing strips the shared-across-pages state that
        # VARIANT_IDENTIFIER requires, so the role must be freed first. Same
        # guard shape (and message style) as delete_object.
        role_blocked = []
        for member in members:
            binding = form_variant_role_binding_repo.get_by_page_object(db, member.id)
            if binding is not None:
                role_blocked.append((member, binding))
        if role_blocked:
            listed = "; ".join(f"{m.id} ({b.role.value})" for m, b in role_blocked)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Cannot unshare: the following object(s) still hold a special "
                    f"role and must be freed first: {listed}."
                ),
            )
        for member in members:
            member.shared_across_pages = False
        db.flush()
        audit_service.log_event(
            db,
            actor_user_id=user.id,
            action=audit_service.PAGE_OBJECT_UNSHARED,
            entity_type="PageObject",
            entity_id=obj.id,
            metadata={"shared_group_id": str(obj.shared_group_id)},
        )
        db.commit()
    db.refresh(obj)
    return build_typed_read(obj)


# ---------------------------------------------------------------------------
# Relink with yes/no choice
#
# Reactivating sharing on a DETACHED object (has a group_id, shared_across_pages
# =False) with other detached copies still around lets the user choose:
#   RECONNECT — re-link those siblings AND overwrite them (base + typed config)
#               from this page (the template); the active object becomes linked.
#   FRESH     — leave the siblings independent; the active object starts a brand
#               new group. Only future pages receive this new group's copy.
# In NEITHER branch is anything created on existing pages.
# ---------------------------------------------------------------------------


def relink_info(db: Session, object_id: uuid.UUID, user: User) -> dict:
    """Report whether *object_id* is detached and how many detached siblings of its
    group remain — so the FE knows whether the yes/no choice is meaningful."""
    obj, _ = load_object_authorized(db, object_id, user)

    is_detached = obj.shared_group_id is not None and not obj.shared_across_pages
    detached_siblings = 0
    if is_detached:
        detached_siblings = sum(
            1
            for m in page_object_repo.list_by_shared_group(db, obj.shared_group_id)
            if m.id != obj.id and not m.shared_across_pages
        )
    return {"is_detached": is_detached, "detached_siblings": detached_siblings}


def relink_object(db: Session, object_id: uuid.UUID, mode: str, user: User):
    """Reactivate sharing on a detached object with an explicit mode (DRAFT only).

    RECONNECT: re-link the group's still-detached siblings and overwrite each from
    the active object — base fields (_PROPAGATED_BASE_FIELDS) + typed config (+
    children) via the typed-config resync helper, IMAGE binaries duplicated.
    The active object becomes linked; its group_id is kept. No existing page is
    copied to.

    FRESH: leave the detached siblings untouched (old group_id, still detached);
    the active object gets a brand-new group_id and becomes linked.

    Already-linked objects are a no-op. An object with no group is promoted into a
    fresh group regardless of mode (defensive — the FE only calls this on the
    detached branch; never-shared promotion goes through share_object).
    """
    obj, _ = load_object_authorized(db, object_id, user, require_draft=True)

    if obj.shared_across_pages and obj.shared_group_id is not None:
        return build_typed_read(obj)  # already linked — nothing to do

    files_to_delete: list[str] = []
    reconnected = 0

    if mode == "RECONNECT" and obj.shared_group_id is not None:
        # Every linked copy is rebuilt from this object's rows.
        _check_identifier_rows_on_relink(
            db, page_object_repo.list_by_shared_group(db, obj.shared_group_id), [obj]
        )
        # YES: re-link this object and overwrite the detached siblings from it.
        obj.shared_across_pages = True
        detached_siblings = [
            m
            for m in page_object_repo.list_by_shared_group(db, obj.shared_group_id)
            if m.id != obj.id and not m.shared_across_pages
        ]
        for sibling in detached_siblings:
            sibling.shared_across_pages = True
            for field in _PROPAGATED_BASE_FIELDS:
                setattr(sibling, field, getattr(obj, field))
        db.flush()
        # Now that the siblings are linked, rebuild their typed config from obj.
        _, files_to_delete = _rebuild_typed_config_for_linked_siblings(db, obj)
        reconnected = len(detached_siblings)
    else:
        # FRESH (or no group): start a new group; siblings stay independent.
        obj.shared_group_id = uuid.uuid4()
        obj.shared_across_pages = True
        db.flush()

    audit_service.log_event(
        db,
        actor_user_id=user.id,
        action=audit_service.PAGE_OBJECT_RELINKED,
        entity_type="PageObject",
        entity_id=obj.id,
        metadata={
            "mode": mode,
            "shared_group_id": str(obj.shared_group_id),
            "siblings_reconnected": reconnected,
        },
    )
    db.commit()
    for image_file in files_to_delete:
        storage_service.delete_object_image(image_file)

    db.refresh(obj)
    return build_typed_read(obj)


def replicate_linked_groups_to_page(
    db: Session, variant_id: uuid.UUID, dst_page_id: uuid.UUID
) -> int:
    """Copy each linked shared group's template onto a freshly created page.

    Called by form_variant_service.add_page, inside its transaction. For every
    linked group of the variant, the deterministic template (lowest page_index
    member) is deep-copied onto *dst_page_id* as a new linked member of the SAME
    group. Returns the number of objects replicated.
    """
    templates = page_object_repo.linked_group_templates_for_variant(
        db, variant_id, exclude_page_id=dst_page_id
    )
    for group_id, template_object_id in templates.items():
        clone_object_to_page(
            db,
            template_object_id,
            dst_page_id,
            shared_group_id=group_id,
            shared_across_pages=True,
        )
    return len(templates)


# ---------------------------------------------------------------------------
# Typed-config resync
#
# Linked members are identical copies. The base fields (geometry + presentation)
# propagate live from each PATCH; the typed config (text/value/fit/question
# +options/matrix+rows/cols/cells) is reconciled here, on the editor's Save, by an
# authoritative BE resync: the saved member is the template and every linked
# sibling's typed config is rebuilt verbatim from it. Detached members and other
# groups are never touched.
# ---------------------------------------------------------------------------


def _typed_config_of(obj: PageObject):
    """Return *obj*'s typed config row for its object_type (or None)."""
    t = obj.object_type
    if t is PageObjectType.TEXT:
        return obj.text_config
    if t is PageObjectType.CODE:
        return obj.code_config
    if t is PageObjectType.FREE_TEXT_AREA:
        return obj.free_text_config
    if t is PageObjectType.QUESTION:
        return obj.question_config
    if t is PageObjectType.MATRIX:
        return obj.matrix_config
    if t is PageObjectType.IMAGE:
        return obj.image_config
    return None


def resync_shared_group_typed_config(
    db: Session, object_id: uuid.UUID, user: User
):
    """Rebuild every linked sibling's typed config from the saved member (DRAFT only).

    No-op when *object_id* is not a linked member. Otherwise the object is the
    template: each linked sibling's current typed config is dropped (children
    cascade via delete-orphan) and recreated verbatim from the template, reusing
    the deep-copy helper (which also duplicates the IMAGE binary). The whole group
    is reconciled atomically; orphaned IMAGE files are removed after commit, the
    same pattern as delete_object.

    Validity note: the template was already reconciled by the granular config
    endpoints (e.g. single-choice, selection_rule) before this resync, so
    copying it verbatim preserves those invariants on the siblings.
    """
    obj, _ = load_object_authorized(db, object_id, user, require_draft=True)

    # Only linked members fan out. Detached / ordinary objects are a no-op.
    if not (obj.shared_across_pages and obj.shared_group_id is not None):
        return build_typed_read(obj)

    resynced, files_to_delete = _rebuild_typed_config_for_linked_siblings(db, obj)

    audit_service.log_event(
        db,
        actor_user_id=user.id,
        action=audit_service.PAGE_OBJECT_TYPED_CONFIG_RESYNCED,
        entity_type="PageObject",
        entity_id=obj.id,
        metadata={
            "shared_group_id": str(obj.shared_group_id),
            "siblings_resynced": resynced,
        },
    )
    db.commit()
    # Remove the siblings' now-orphaned IMAGE binaries only after the DB delete is
    # committed (same ordering as delete_object).
    for image_file in files_to_delete:
        storage_service.delete_object_image(image_file)

    db.refresh(obj)
    return build_typed_read(obj)


def _rebuild_typed_config_for_linked_siblings(
    db: Session, obj: PageObject
) -> tuple[int, list[str]]:
    """Rebuild every linked sibling's typed config (+ children) from *obj*.

    *obj* is the template. Each linked sibling (same group, shared_across_pages,
    id != obj.id) has its typed config dropped (children cascade via delete-orphan)
    and recreated verbatim from obj, duplicating the IMAGE binary. Flushes but does
    NOT commit — the caller owns the transaction and audit. Returns
    ``(siblings_rebuilt, orphaned_image_files)``; the caller removes the files
    AFTER the commit (same ordering as delete_object).

    Shared by the save resync and the RECONNECT relink.
    """
    template = page_object_repo.get_with_configs(db, obj.id)
    siblings = [
        m
        for m in page_object_repo.list_by_shared_group(db, obj.shared_group_id)
        if m.shared_across_pages and m.id != obj.id
    ]

    files_to_delete: list[str] = []
    for sibling in siblings:
        # Capture the sibling's current IMAGE binary BEFORE its config is dropped.
        old_image_file: str | None = None
        if (
            sibling.object_type is PageObjectType.IMAGE
            and sibling.image_config is not None
        ):
            old_image_file = sibling.image_config.file_path

        old_config = _typed_config_of(sibling)
        if old_config is not None:
            # Drop the config row + its children (QUESTION options / MATRIX
            # rows/cols/cells, via all/delete-orphan cascade). Flush so the DELETE
            # precedes the rebuild INSERT (config tables are unique per object).
            db.delete(old_config)
            db.flush()
        _clone_typed_config(db, template, sibling.id)

        # The rebuilt IMAGE binary is written at an id+extension-deterministic
        # path. When the extension is unchanged that is the SAME path as the old
        # file, which copy_object_image just overwrote in place — so only the
        # old file at a *different* path is a genuine orphan to remove.
        if old_image_file:
            new_image_file = _resynced_image_target_path(template, sibling.id)
            if old_image_file != new_image_file:
                files_to_delete.append(old_image_file)
    db.flush()
    return len(siblings), files_to_delete


def _resynced_image_target_path(
    template: PageObject, sibling_id: uuid.UUID
) -> str | None:
    """Path where a resync rebuilds *sibling_id*'s IMAGE binary from *template*.

    Mirrors _clone_image_config: the new file lands at object_image_path(sibling
    id, template's extension). None when the template carries no file. Used to
    decide whether the sibling's old file was overwritten in place or orphaned.
    """
    cfg = template.image_config
    if cfg is None or not cfg.file_path:
        return None
    return str(
        storage_service.object_image_path(sibling_id, PurePath(cfg.file_path).suffix)
    )


# ---------------------------------------------------------------------------
# Deep copy (used by the FormVariant clone path)
#
# A clone must be a fully independent copy: every PageObject is recreated with
# its typed config and all children, each getting a fresh UUID and fresh
# timestamps (ids/timestamps are NEVER carried over). Because each copy points
# at the new object/config ids, later edits to the clone cannot reach back into
# the source. This mirrors the explicit-FK construction used by the typed
# create_* services rather than relying on ORM relationship cascades.
# ---------------------------------------------------------------------------


def clone_objects_to_page(
    db: Session,
    source_page_id: uuid.UUID,
    target_page_id: uuid.UUID,
    *,
    group_id_map: dict[uuid.UUID, uuid.UUID] | None = None,
) -> int:
    """Deep-copy every PageObject of *source_page_id* onto *target_page_id*.

    Returns the number of PageObjects copied. Authorization is the caller's
    responsibility — the clone path authorizes once at the variant level — so
    this helper performs no access checks of its own.

    *group_id_map*: when cloning a whole variant, shared groups must
    NOT be copied verbatim (that would share a group across variants). The caller
    passes ONE map, reused across all pages of the clone, so source group ids are
    rewritten to fresh ids consistently — members of the same source group stay in
    the same (new) group, and shared_across_pages is preserved. When the map is
    None, group membership is dropped entirely (a fully page-local copy).
    """
    sources = page_object_repo.list_for_page_with_configs(db, source_page_id)
    for src in sources:
        new_group_id: uuid.UUID | None = None
        if src.shared_group_id is not None and group_id_map is not None:
            new_group_id = group_id_map.setdefault(src.shared_group_id, uuid.uuid4())
        _clone_object_row(
            db,
            src,
            target_page_id,
            shared_group_id=new_group_id,
            # An object can only be linked if it landed in a (remapped) group.
            shared_across_pages=src.shared_across_pages and new_group_id is not None,
        )
    return len(sources)


def clone_object_to_page(
    db: Session,
    src_object_id: uuid.UUID,
    dst_page_id: uuid.UUID,
    *,
    shared_group_id: uuid.UUID | None,
    shared_across_pages: bool,
) -> PageObject:
    """Deep-copy a SINGLE object (typed config + children) onto another page.

    Reuses the same per-object copy logic as the variant-clone path, but lets the
    caller pin the shared-group fields — used by the lazy replication that copies
    a linked group's template onto a freshly added page. Authorization
    is the caller's responsibility.
    """
    src = page_object_repo.get_with_configs(db, src_object_id)
    if src is None:  # defensive — caller resolved this id moments ago
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Page object not found"
        )
    return _clone_object_row(
        db,
        src,
        dst_page_id,
        shared_group_id=shared_group_id,
        shared_across_pages=shared_across_pages,
    )


def duplicate_objects_to_page(
    db: Session, source_page_id: uuid.UUID, target_page_id: uuid.UUID
) -> int:
    """Copy every object of *source_page_id* onto *target_page_id* for PAGE
    DUPLICATION, with a PER-OBJECT group rule (distinct from
    clone_objects_to_page, which remaps groups to isolate a cloned VARIANT):

      * live linked member (shared_across_pages and shared_group_id is not None) →
        the copy JOINS THE SAME group as a new linked member (group_id PRESERVED,
        shared_across_pages=True), so it stays live-synced with the source;
      * ordinary or detached object → independent page-local deep copy
        (shared_group_id=None, shared_across_pages=False), so editing it never
        touches the source.

    This is option (a) "identical to source": the duplicate gets EXACTLY the
    source page's objects — no lazy "show on all pages" replication is triggered,
    so groups not present on the source page do NOT appear on the duplicate.

    Reuses _clone_object_row (typed config + IMAGE binary duplication included).
    Authorization is the caller's responsibility (the duplicate path authorizes at
    the variant level). Returns the number of objects copied.
    """
    sources = page_object_repo.list_for_page_with_configs(db, source_page_id)
    for src in sources:
        if src.shared_across_pages and src.shared_group_id is not None:
            _clone_object_row(
                db,
                src,
                target_page_id,
                shared_group_id=src.shared_group_id,
                shared_across_pages=True,
            )
        else:
            _clone_object_row(
                db,
                src,
                target_page_id,
                shared_group_id=None,
                shared_across_pages=False,
            )
    return len(sources)


def _clone_object_row(
    db: Session,
    src: PageObject,
    target_page_id: uuid.UUID,
    *,
    shared_group_id: uuid.UUID | None,
    shared_across_pages: bool,
) -> PageObject:
    """Create one deep copy of *src* on *target_page_id* with the given group fields."""
    new_obj = page_object_repo.add(
        db,
        PageObject(
            form_variant_page_id=target_page_id,
            object_type=src.object_type,
            x=src.x,
            y=src.y,
            width=src.width,
            height=src.height,
            z_index=src.z_index,
            label=src.label,
            description=src.description,
            visible=src.visible,
            locked=src.locked,
            shared_group_id=shared_group_id,
            shared_across_pages=shared_across_pages,
        ),
    )
    _clone_typed_config(db, src, new_obj.id)
    return new_obj


def _clone_typed_config(
    db: Session, src: PageObject, new_object_id: uuid.UUID
) -> None:
    """Copy *src*'s typed config (+ children) onto the freshly created object."""
    t = src.object_type
    if t is PageObjectType.TEXT:
        _clone_text_config(db, src.text_config, new_object_id)
    elif t is PageObjectType.CODE:
        _clone_code_config(db, src.code_config, new_object_id)
    elif t is PageObjectType.FREE_TEXT_AREA:
        _clone_free_text_config(db, src.free_text_config, new_object_id)
    elif t is PageObjectType.QUESTION:
        _clone_question_config(db, src.question_config, new_object_id)
    elif t is PageObjectType.MATRIX:
        _clone_matrix_config(db, src.matrix_config, new_object_id)
    elif t is PageObjectType.IMAGE:
        _clone_image_config(db, src.image_config, new_object_id)


def _clone_text_config(
    db: Session, cfg: TextObjectConfig | None, new_object_id: uuid.UUID
) -> None:
    if cfg is None:  # defensive — a well-formed TEXT object always has one
        return
    text_object_repo.add(
        db,
        TextObjectConfig(
            page_object_id=new_object_id,
            text=cfg.text,
            font_size=cfg.font_size,
            font_family=cfg.font_family,
            bold=cfg.bold,
            italic=cfg.italic,
        ),
    )


def _clone_code_config(
    db: Session, cfg: CodeObjectConfig | None, new_object_id: uuid.UUID
) -> None:
    if cfg is None:
        return
    code_object_repo.add(
        db,
        CodeObjectConfig(
            page_object_id=new_object_id,
            code_type=cfg.code_type,
            encoding=cfg.encoding,
            value=cfg.value,
            human_readable=cfg.human_readable,
            # This clone is a field-by-field copy (not a verbatim row copy), so
            # every CODE field must be listed explicitly for it to propagate to
            # linked siblings on resync.
            include_page_index=cfg.include_page_index,
        ),
    )


def _clone_free_text_config(
    db: Session, cfg: FreeTextAreaConfig | None, new_object_id: uuid.UUID
) -> None:
    if cfg is None:
        return
    free_text_area_repo.add(
        db,
        FreeTextAreaConfig(
            page_object_id=new_object_id,
            placeholder=cfg.placeholder,
            max_length=cfg.max_length,
        ),
    )


def _clone_image_config(
    db: Session, cfg: ImageObjectConfig | None, new_object_id: uuid.UUID
) -> None:
    if cfg is None:
        return
    # Duplicate the binary so the cloned variant owns an independent file (a
    # later delete of one variant never touches the other's image). If the
    # source file is missing, clone the config without a file rather than
    # failing the whole variant clone.
    new_file_path: str | None = None
    if cfg.file_path:
        try:
            copied = storage_service.copy_object_image(
                cfg.file_path, new_object_id, PurePath(cfg.file_path).suffix
            )
            new_file_path = str(copied)
        except (ValueError, OSError):
            new_file_path = None
    has_file = new_file_path is not None
    image_object_repo.add(
        db,
        ImageObjectConfig(
            page_object_id=new_object_id,
            file_path=new_file_path,
            original_filename=cfg.original_filename if has_file else None,
            mime_type=cfg.mime_type if has_file else None,
            width_px=cfg.width_px if has_file else None,
            height_px=cfg.height_px if has_file else None,
            fit=cfg.fit,
        ),
    )


def _clone_question_config(
    db: Session, cfg: QuestionObjectConfig | None, new_object_id: uuid.UUID
) -> None:
    if cfg is None:
        return
    new_cfg = question_object_repo.add_config(
        db,
        QuestionObjectConfig(
            page_object_id=new_object_id,
            question_type=cfg.question_type,
            question_text=cfg.question_text,
            shuffle_answers=cfg.shuffle_answers,
        ),
    )
    for opt in cfg.options:
        question_object_repo.add_option(
            db,
            QuestionOption(
                question_object_config_id=new_cfg.id,
                order_index=opt.order_index,
                option_text=opt.option_text,
                is_correct=opt.is_correct,
                score=opt.score,
            ),
        )


def _clone_matrix_config(
    db: Session, cfg: MatrixObjectConfig | None, new_object_id: uuid.UUID
) -> None:
    if cfg is None:
        return
    new_cfg = matrix_object_repo.add_config(
        db,
        MatrixObjectConfig(
            page_object_id=new_object_id,
            rows_count=cfg.rows_count,
            cols_count=cfg.cols_count,
            evaluative=cfg.evaluative,
            selection_rule=cfg.selection_rule,
        ),
    )
    for row in cfg.rows:
        matrix_object_repo.add_row(
            db,
            MatrixRowDef(
                matrix_object_config_id=new_cfg.id,
                order_index=row.order_index,
                label=row.label,
            ),
        )
    for col in cfg.cols:
        matrix_object_repo.add_col(
            db,
            MatrixColDef(
                matrix_object_config_id=new_cfg.id,
                order_index=col.order_index,
                label=col.label,
            ),
        )
    for cell in cfg.cells:
        matrix_object_repo.add_cell(
            db,
            MatrixCellDef(
                matrix_object_config_id=new_cfg.id,
                row_index=cell.row_index,
                col_index=cell.col_index,
                is_correct=cell.is_correct,
                score=cell.score,
            ),
        )
