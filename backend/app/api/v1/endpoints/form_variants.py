# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""FormVariant endpoints.

Two routers:
  - projects_router: project-scoped collection routes, mounted at /form-projects
      GET  /form-projects/{project_id}/variants
      POST /form-projects/{project_id}/variants
  - router: variant-scoped item routes, mounted at /form-variants
      GET    /form-variants/{variant_id}
      PATCH  /form-variants/{variant_id}
      DELETE /form-variants/{variant_id}
      POST   /form-variants/{variant_id}/publish
      POST   /form-variants/{variant_id}/clone
      GET    /form-variants/{variant_id}/pages
      POST   /form-variants/{variant_id}/pages
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.deps import get_db, require_user
from app.models.user import User
from app.schemas.form_variant import (
    FormVariantCreate,
    FormVariantDetail,
    FormVariantListItem,
    FormVariantPageRead,
    FormVariantPageReorder,
    FormVariantRead,
    FormVariantUpdate,
    VariantMaxScoreRead,
)
from app.schemas.page_object import PageObjectRoleRead
from app.services import (
    form_variant_service,
    page_object_role_service,
    variant_max_score_service,
)

# Mounted at /form-projects — variant collection under a project.
projects_router = APIRouter()
# Mounted at /form-variants — single-variant operations.
router = APIRouter()


# ---------------------------------------------------------------------------
# Collection routes (under /form-projects/{project_id}/variants)
# ---------------------------------------------------------------------------


@projects_router.get(
    "/{project_id}/variants", response_model=list[FormVariantListItem]
)
def list_variants(
    project_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """List variants of a project (owner or admin)."""
    return form_variant_service.list_variants(db, project_id, user)


@projects_router.post(
    "/{project_id}/variants",
    response_model=FormVariantRead,
    status_code=status.HTTP_201_CREATED,
)
def create_variant(
    project_id: uuid.UUID,
    data: FormVariantCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Create a new DRAFT variant; auto-provisions one blank A4 first page."""
    variant = form_variant_service.create_variant(db, project_id, data, user)
    return FormVariantRead.model_validate(variant)


# ---------------------------------------------------------------------------
# Item routes (under /form-variants/{variant_id})
# ---------------------------------------------------------------------------


@router.get("/{variant_id}", response_model=FormVariantDetail)
def get_variant(
    variant_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Get a single variant with its pages embedded."""
    return form_variant_service.get_variant_detail(db, variant_id, user)


@router.patch("/{variant_id}", response_model=FormVariantRead)
def update_variant(
    variant_id: uuid.UUID,
    data: FormVariantUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Update name/description of a DRAFT variant. Raises 409 if not DRAFT."""
    variant = form_variant_service.update_variant(db, variant_id, data, user)
    return FormVariantRead.model_validate(variant)


@router.delete("/{variant_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_variant(
    variant_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Delete a DRAFT variant and all its objects (cascade).

    Returns 409 if the variant is not DRAFT (PUBLISHED/ARCHIVED are not
    deletable). On success the variant, its pages, every PageObject and their
    typed configs are removed atomically.
    """
    form_variant_service.delete_variant(db, variant_id, user)


@router.post(
    "/{variant_id}/publish",
    response_model=FormVariantRead,
    status_code=status.HTTP_200_OK,
)
def publish_variant(
    variant_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Publish a DRAFT variant. Gates: at least one page, a VARIANT_IDENTIFIER
    value not already published by a sibling, and no object in the reserved
    bands (409 ``OBJECTS_IN_RESERVED_BANDS`` with ``objects``)."""
    variant = form_variant_service.publish_variant(db, variant_id, user)
    return FormVariantRead.model_validate(variant)


@router.post(
    "/{variant_id}/clone",
    response_model=FormVariantRead,
    status_code=status.HTTP_201_CREATED,
)
def clone_variant(
    variant_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Clone a variant as a new DRAFT.

    Copies metadata, mirrors the source's pages, and deep-copies every
    PageObject (with its typed config + children) onto the clone.
    """
    variant = form_variant_service.clone_variant(db, variant_id, user)
    return FormVariantRead.model_validate(variant)


@router.get("/{variant_id}/pages", response_model=list[FormVariantPageRead])
def list_variant_pages(
    variant_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """List the pages of a variant."""
    return form_variant_service.list_pages(db, variant_id, user)


@router.get(
    "/{variant_id}/role-bindings", response_model=list[PageObjectRoleRead]
)
def list_variant_role_bindings(
    variant_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """List the variant's special scanning-role bindings (0, 1 or 2 rows).

    Which PageObject, if any, is the VARIANT_IDENTIFIER /
    RESPONDENT_IDENTIFIER of this variant.
    """
    return page_object_role_service.list_variant_role_bindings(
        db, variant_id, user
    )


@router.get("/{variant_id}/max-score", response_model=VariantMaxScoreRead)
def get_variant_max_score(
    variant_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """The maximum score a sheet of this variant can earn.

    Computed on the fly from the authored scores, in any status of the variant.
    """
    return variant_max_score_service.get_max_score(db, variant_id, user)


@router.post(
    "/{variant_id}/pages",
    response_model=FormVariantPageRead,
    status_code=status.HTTP_201_CREATED,
)
def add_variant_page(
    variant_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Append a blank A4 page to a DRAFT variant.

    No request body: the page is server-provisioned (blank A4) and its
    page_index is computed server-side (max existing + 1). Raises 409 if the
    variant is not DRAFT (PUBLISHED/ARCHIVED are read-only).
    """
    page = form_variant_service.add_page(db, variant_id, user)
    return FormVariantPageRead.model_validate(page)


@router.post(
    "/{variant_id}/pages/{page_id}/duplicate",
    response_model=FormVariantPageRead,
    status_code=status.HTTP_201_CREATED,
)
def duplicate_variant_page(
    variant_id: uuid.UUID,
    page_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Duplicate one page of a DRAFT variant (content-replicating, group-aware).

    Creates a new page whose content replicates the source page, inserted
    immediately after the source, then renumbers 1..n. Per object: a live
    linked-group member joins the SAME group as a new linked member (live sync);
    an ordinary or detached object becomes an independent copy. No lazy
    "show on all pages" replication runs, so the duplicate is identical to the
    source. Raises 409 if the variant is not DRAFT (PUBLISHED/
    ARCHIVED are read-only), 404 if the page does not belong to the variant.
    """
    page = form_variant_service.duplicate_page(db, variant_id, page_id, user)
    return FormVariantPageRead.model_validate(page)


@router.patch(
    "/{variant_id}/pages/reorder", response_model=list[FormVariantPageRead]
)
def reorder_variant_pages(
    variant_id: uuid.UUID,
    data: FormVariantPageReorder,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Reorder a DRAFT variant's pages and renumber 1..n.

    The body is a full permutation of the variant's current page ids. Raises 409
    if the variant is not DRAFT (PUBLISHED/ARCHIVED are read-only) and 400 if the
    ids are not exactly the variant's pages. Returns the reordered page list.
    """
    return form_variant_service.reorder_pages(
        db, variant_id, data.ordered_page_ids, user
    )


@router.delete(
    "/{variant_id}/pages/{page_id}", response_model=list[FormVariantPageRead]
)
def delete_variant_page(
    variant_id: uuid.UUID,
    page_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Remove one page of a DRAFT variant (scoped delete) and renumber 1..n.

    Raises 409 if the variant is not DRAFT or if it is the variant's only page,
    404 if the page does not belong to the variant. Linked objects on other pages
    are preserved. Returns the remaining page list.
    """
    return form_variant_service.delete_page(db, variant_id, page_id, user)
