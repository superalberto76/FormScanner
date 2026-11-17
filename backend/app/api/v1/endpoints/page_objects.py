# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""PageObject endpoints.

Routers:
  - pages_router  (mounted at /form-variant-pages)
      GET  /{page_id}/objects
      POST /{page_id}/objects/text | code | free-text-area | question | matrix
  - router        (mounted at /page-objects)
      GET/PATCH/DELETE /{object_id}
      PATCH /{object_id}/text-config | code-config | free-text-config
            | question-config | matrix-config
      GET/POST /{object_id}/question-options
      GET/POST /{object_id}/matrix-rows | matrix-cols | matrix-cells
"""
from __future__ import annotations

import re
import uuid
from typing import Annotated, Union

from fastapi import APIRouter, Depends, File, Response, UploadFile, status
from pydantic import Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import get_db, require_user
from app.models.user import User
from app.schemas.matrix_object import (
    MatrixCellDefCreate,
    MatrixCellDefRead,
    MatrixColDefCreate,
    MatrixColDefRead,
    MatrixObjectConfigUpdate,
    MatrixPageObjectCreate,
    MatrixPageObjectRead,
    MatrixRowDefCreate,
    MatrixRowDefRead,
)
from app.schemas.page_object import (
    CodeObjectConfigUpdate,
    CodePageObjectCreate,
    CodePageObjectRead,
    FreeTextAreaConfigUpdate,
    FreeTextAreaPageObjectCreate,
    FreeTextAreaPageObjectRead,
    ImageObjectConfigUpdate,
    ImagePageObjectCreate,
    ImagePageObjectRead,
    PageObjectRoleAssign,
    PageObjectUpdate,
    PageObjectWithConfig,
    RelinkInfo,
    RelinkRequest,
    TextObjectConfigUpdate,
    TextPageObjectCreate,
    TextPageObjectRead,
)
from app.schemas.question_object import (
    QuestionObjectConfigUpdate,
    QuestionOptionCreate,
    QuestionOptionRead,
    QuestionPageObjectCreate,
    QuestionPageObjectRead,
)
from app.services import (
    code_object_service,
    free_text_area_service,
    image_object_service,
    matrix_object_service,
    page_object_role_service,
    page_object_service,
    question_object_service,
    text_object_service,
)

pages_router = APIRouter()   # mounted at /form-variant-pages
router = APIRouter()         # mounted at /page-objects

# Discriminated union: GET /page-objects/{id} returns the variant matching
# object_type. Each typed read pins object_type to a Literal discriminator.
PageObjectTypedRead = Annotated[
    Union[
        TextPageObjectRead,
        CodePageObjectRead,
        FreeTextAreaPageObjectRead,
        QuestionPageObjectRead,
        MatrixPageObjectRead,
        ImagePageObjectRead,
    ],
    Field(discriminator="object_type"),
]


# ===========================================================================
# Page-scoped: list + typed create
# ===========================================================================


@pages_router.get("/{page_id}/objects", response_model=list[PageObjectWithConfig])
def list_page_objects(
    page_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """List all objects of a page, each with its typed config, ordered by z_index
    then created_at. The enriched config lets the canvas render content previews
    in a single request."""
    return page_object_service.list_objects(db, page_id, user)


@pages_router.post(
    "/{page_id}/objects/text",
    response_model=TextPageObjectRead,
    status_code=status.HTTP_201_CREATED,
)
def create_text_object(
    page_id: uuid.UUID,
    data: TextPageObjectCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    return text_object_service.create_text(db, page_id, data, user)


@pages_router.post(
    "/{page_id}/objects/code",
    response_model=CodePageObjectRead,
    status_code=status.HTTP_201_CREATED,
)
def create_code_object(
    page_id: uuid.UUID,
    data: CodePageObjectCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    return code_object_service.create_code(db, page_id, data, user)


@pages_router.post(
    "/{page_id}/objects/free-text-area",
    response_model=FreeTextAreaPageObjectRead,
    status_code=status.HTTP_201_CREATED,
)
def create_free_text_area_object(
    page_id: uuid.UUID,
    data: FreeTextAreaPageObjectCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    return free_text_area_service.create_free_text_area(db, page_id, data, user)


@pages_router.post(
    "/{page_id}/objects/question",
    response_model=QuestionPageObjectRead,
    status_code=status.HTTP_201_CREATED,
)
def create_question_object(
    page_id: uuid.UUID,
    data: QuestionPageObjectCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    return question_object_service.create_question(db, page_id, data, user)


@pages_router.post(
    "/{page_id}/objects/matrix",
    response_model=MatrixPageObjectRead,
    status_code=status.HTTP_201_CREATED,
)
def create_matrix_object(
    page_id: uuid.UUID,
    data: MatrixPageObjectCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    return matrix_object_service.create_matrix(db, page_id, data, user)


@pages_router.post(
    "/{page_id}/objects/image",
    response_model=ImagePageObjectRead,
    status_code=status.HTTP_201_CREATED,
)
def create_image_object(
    page_id: uuid.UUID,
    data: ImagePageObjectCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Create an IMAGE object (geometry + fit). The file is uploaded separately
    via POST /page-objects/{id}/image."""
    return image_object_service.create_image(db, page_id, data, user)


# ===========================================================================
# Object-scoped: read / update geometry / delete
# ===========================================================================


@router.get("/{object_id}", response_model=PageObjectTypedRead)
def get_page_object(
    object_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Get a single object as its typed read model (discriminated by object_type)."""
    return page_object_service.get_object(db, object_id, user)


@router.patch("/{object_id}", response_model=PageObjectTypedRead)
def update_page_object(
    object_id: uuid.UUID,
    data: PageObjectUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Update geometry/presentation (x,y,w,h,z_index,label,description,visible,locked).

    Requires the parent variant to be DRAFT (else 409).
    """
    return page_object_service.update_object(db, object_id, data, user)


@router.delete("/{object_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_page_object(
    object_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Delete an object (and its typed config + children). DRAFT only.

    Deleting a LINKED member removes the whole linked group (the object on every
    page it is shared on); detached/ordinary objects delete just themselves.
    """
    page_object_service.delete_object(db, object_id, user)


# ===========================================================================
# Share / unshare ("show on all pages"). DRAFT only.
# ===========================================================================


@router.post("/{object_id}/share", response_model=PageObjectTypedRead)
def share_page_object(
    object_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Link an object so it is shown on all pages (becomes its group's template).

    Does NOT copy it onto existing pages — only new pages receive a linked copy.
    Requires the parent variant to be DRAFT (else 409).
    """
    return page_object_service.share_object(db, object_id, user)


@router.delete("/{object_id}/share", response_model=PageObjectTypedRead)
def unshare_page_object(
    object_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Unlink: detach every member of the group (existing copies stay put). DRAFT only."""
    return page_object_service.unshare_object(db, object_id, user)


@router.post("/{object_id}/resync-shared", response_model=PageObjectTypedRead)
def resync_shared_page_object(
    object_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Rebuild every linked sibling's typed config from this saved member.

    Called by the editor after a Save that changed the typed config/children, so
    the other pages' copies stay identical. No-op for a non-linked object. DRAFT
    only (enforced by the service).
    """
    return page_object_service.resync_shared_group_typed_config(db, object_id, user)


@router.get("/{object_id}/relink-info", response_model=RelinkInfo)
def get_relink_info(
    object_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Whether the object is detached and how many detached siblings of its group
    remain — drives the FE yes/no relink modal."""
    return page_object_service.relink_info(db, object_id, user)


@router.post("/{object_id}/relink", response_model=PageObjectTypedRead)
def relink_page_object(
    object_id: uuid.UUID,
    data: RelinkRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Reactivate sharing on a detached object. DRAFT only.

    mode=RECONNECT re-links the group's detached siblings and overwrites them
    (base + typed config) from this page; mode=FRESH starts a brand-new group and
    leaves the old siblings independent. Neither branch copies onto existing pages.
    """
    return page_object_service.relink_object(db, object_id, data.mode, user)


# ===========================================================================
# Special scanning role. DRAFT only.
# ===========================================================================


@router.put("/{object_id}/role", response_model=PageObjectTypedRead)
def assign_page_object_role(
    object_id: uuid.UUID,
    data: PageObjectRoleAssign,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Assign a special role (VARIANT_IDENTIFIER / RESPONDENT_IDENTIFIER).

    409 if the object type is not allowed for the role, if the object already
    holds a role, or if the role is already held by another object of the same
    variant. Requires the parent variant to be DRAFT.
    """
    return page_object_role_service.assign_role(db, object_id, data.role, user)


@router.delete("/{object_id}/role", status_code=status.HTTP_204_NO_CONTENT)
def remove_page_object_role(
    object_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Remove the object's special role. 404 if it has none. DRAFT only."""
    page_object_role_service.remove_role(db, object_id, user)


# ===========================================================================
# Typed config update endpoints
# ===========================================================================


@router.patch("/{object_id}/text-config", response_model=TextPageObjectRead)
def update_text_config(
    object_id: uuid.UUID,
    data: TextObjectConfigUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    return text_object_service.update_text_config(db, object_id, data, user)


@router.patch("/{object_id}/code-config", response_model=CodePageObjectRead)
def update_code_config(
    object_id: uuid.UUID,
    data: CodeObjectConfigUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    return code_object_service.update_code_config(db, object_id, data, user)


@router.patch("/{object_id}/free-text-config", response_model=FreeTextAreaPageObjectRead)
def update_free_text_config(
    object_id: uuid.UUID,
    data: FreeTextAreaConfigUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    return free_text_area_service.update_free_text_config(db, object_id, data, user)


@router.patch("/{object_id}/question-config", response_model=QuestionPageObjectRead)
def update_question_config(
    object_id: uuid.UUID,
    data: QuestionObjectConfigUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    return question_object_service.update_question_config(db, object_id, data, user)


@router.patch("/{object_id}/matrix-config", response_model=MatrixPageObjectRead)
def update_matrix_config(
    object_id: uuid.UUID,
    data: MatrixObjectConfigUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    return matrix_object_service.update_matrix_config(db, object_id, data, user)


@router.patch("/{object_id}/image-config", response_model=ImagePageObjectRead)
def update_image_config(
    object_id: uuid.UUID,
    data: ImageObjectConfigUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    return image_object_service.update_image_config(db, object_id, data, user)


# ===========================================================================
# IMAGE file upload / serving
# ===========================================================================


def _content_disposition_filename(name: str) -> str:
    """ASCII-safe filename for a Content-Disposition header (no header injection)."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")
    return safe or "image"


@router.post("/{object_id}/image", response_model=ImagePageObjectRead)
async def upload_object_image(
    object_id: uuid.UUID,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Upload (or replace) the binary for an IMAGE object.

    Validation is authoritative here: real MIME sniffed from content, size cap,
    SVG sanitization, server-generated filename. Reads at most max+1 bytes so an
    oversize upload is rejected (413) without buffering the whole file.
    """
    data = await file.read(settings.max_image_upload_bytes + 1)
    return image_object_service.upload_image(db, object_id, data, file.filename, user)


@router.get("/{object_id}/image")
def get_object_image(
    object_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Serve an IMAGE object's binary with the correct Content-Type.

    Hardened for SVG: ``nosniff`` + a locked-down CSP with ``sandbox`` (no
    scripts) so a browser cannot execute an SVG even if one slipped past the
    sanitizer; served ``inline`` (the FE renders it through an <img>).
    """
    content, mime, filename = image_object_service.get_image_file(db, object_id, user)
    disp = _content_disposition_filename(filename or f"image-{object_id}")
    return Response(
        content=content,
        media_type=mime,
        headers={
            "Content-Disposition": f'inline; filename="{disp}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
            "Cache-Control": "private, max-age=0, must-revalidate",
        },
    )


# ===========================================================================
# Question options (collection under an object)
# ===========================================================================


@router.get("/{object_id}/question-options", response_model=list[QuestionOptionRead])
def list_question_options(
    object_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    return question_object_service.list_options(db, object_id, user)


@router.post(
    "/{object_id}/question-options",
    response_model=QuestionOptionRead,
    status_code=status.HTTP_201_CREATED,
)
def create_question_option(
    object_id: uuid.UUID,
    data: QuestionOptionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    return question_object_service.create_option(db, object_id, data, user)


# ===========================================================================
# Matrix rows / cols / cells (collections under an object)
# ===========================================================================


@router.get("/{object_id}/matrix-rows", response_model=list[MatrixRowDefRead])
def list_matrix_rows(
    object_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    return matrix_object_service.list_rows(db, object_id, user)


@router.post(
    "/{object_id}/matrix-rows",
    response_model=MatrixRowDefRead,
    status_code=status.HTTP_201_CREATED,
)
def create_matrix_row(
    object_id: uuid.UUID,
    data: MatrixRowDefCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    return matrix_object_service.create_row(db, object_id, data, user)


@router.get("/{object_id}/matrix-cols", response_model=list[MatrixColDefRead])
def list_matrix_cols(
    object_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    return matrix_object_service.list_cols(db, object_id, user)


@router.post(
    "/{object_id}/matrix-cols",
    response_model=MatrixColDefRead,
    status_code=status.HTTP_201_CREATED,
)
def create_matrix_col(
    object_id: uuid.UUID,
    data: MatrixColDefCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    return matrix_object_service.create_col(db, object_id, data, user)


@router.get("/{object_id}/matrix-cells", response_model=list[MatrixCellDefRead])
def list_matrix_cells(
    object_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    return matrix_object_service.list_cells(db, object_id, user)


@router.post(
    "/{object_id}/matrix-cells",
    response_model=MatrixCellDefRead,
    status_code=status.HTTP_201_CREATED,
)
def create_matrix_cell(
    object_id: uuid.UUID,
    data: MatrixCellDefCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    return matrix_object_service.create_cell(db, object_id, data, user)
