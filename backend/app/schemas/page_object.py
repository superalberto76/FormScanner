# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pydantic schemas for PageObject and the TEXT / CODE / FREE_TEXT_AREA typed
configs.

QUESTION and MATRIX typed schemas live in their own modules
(``question_object.py`` / ``matrix_object.py``) to keep this file focused and
avoid import cycles — those modules import the base classes defined here.
"""
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import CodeEncoding, CodeType, PageObjectRole, PageObjectType


# ---------------------------------------------------------------------------
# Shared PageObject geometry / base
# ---------------------------------------------------------------------------


class PageObjectCreateBase(BaseModel):
    """Geometry + presentation fields shared by every typed create request.

    ``object_type`` is NOT part of the body — it is implied by the create URL
    (e.g. POST .../objects/text). x/y must be >= 0; width/height must be > 0.
    """

    x: float = Field(ge=0)
    y: float = Field(ge=0)
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    z_index: int = 0
    label: str | None = None
    description: str | None = None
    visible: bool = True
    locked: bool = False
    # "Show on all pages": when True the object is created as the
    # template of a fresh linked group. Existing pages are NOT copied to — only
    # pages added later receive a linked copy.
    shared_across_pages: bool = False


class PageObjectUpdate(BaseModel):
    """Geometry / presentation PATCH. Typed-config fields use dedicated endpoints."""

    x: float | None = Field(default=None, ge=0)
    y: float | None = Field(default=None, ge=0)
    width: float | None = Field(default=None, gt=0)
    height: float | None = Field(default=None, gt=0)
    z_index: int | None = None
    label: str | None = None
    description: str | None = None
    visible: bool | None = None
    locked: bool | None = None


class PageObjectBaseRead(BaseModel):
    """Base read model — geometry + presentation, no typed config."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    form_variant_page_id: uuid.UUID
    object_type: PageObjectType
    # Optional special scanning role (Identification). Read straight off the
    # PageObject.role property (its role_binding), so every typed read
    # and PageObjectWithConfig that inherit this base expose it for free.
    role: PageObjectRole | None = None
    x: float
    y: float
    width: float
    height: float
    z_index: int
    label: str | None = None
    description: str | None = None
    visible: bool
    locked: bool
    # Shared "template page" object. group_id + shared_across_pages
    # together describe whether this row is a linked / detached / ordinary object.
    shared_group_id: uuid.UUID | None = None
    shared_across_pages: bool = False
    created_at: datetime
    updated_at: datetime


class PageObjectListItem(BaseModel):
    """Simplified row for GET /form-variant-pages/{id}/objects.

    Deliberately base-only (no typed config) — clients fetch the full typed
    read via GET /page-objects/{id} when they open an object for editing.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    form_variant_page_id: uuid.UUID
    object_type: PageObjectType
    label: str | None = None
    z_index: int
    x: float
    y: float
    width: float
    height: float
    visible: bool
    locked: bool
    # Shared "template page" object — lets the list/overlay flag
    # linked objects without a second fetch.
    shared_group_id: uuid.UUID | None = None
    shared_across_pages: bool = False
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Relink — reactivating sharing on a DETACHED object
# ---------------------------------------------------------------------------


# RECONNECT = "yes" (re-link detached siblings and overwrite them from this page);
# FRESH = "no" (leave siblings independent; this object starts a new group).
RelinkMode = Literal["RECONNECT", "FRESH"]


class RelinkInfo(BaseModel):
    """Drives the FE yes/no modal: is this a detached object, and do other detached
    copies of its group still exist (so the choice actually matters)?"""

    is_detached: bool
    detached_siblings: int


class RelinkRequest(BaseModel):
    mode: RelinkMode


# ---------------------------------------------------------------------------
# Special scanning role (Identification)
#
# A PageObject may hold at most one optional role within its variant. The
# assign body carries only the role; the read mirrors the FormVariantRoleBinding
# row (used by GET /form-variants/{id}/role-bindings).
# ---------------------------------------------------------------------------


class PageObjectRoleAssign(BaseModel):
    role: PageObjectRole


class PageObjectRoleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    form_variant_id: uuid.UUID
    page_object_id: uuid.UUID
    role: PageObjectRole
    # Enrichment for the Identification summary: the bound object's
    # label (verbatim, may be None — the FE decides how to present it, no text
    # fallback here) and the 0-based index of the page that hosts it. Read
    # straight off the FormVariantRoleBinding properties via from_attributes.
    object_label: str | None = None
    page_index: int
    # The bound object's shared-group id, or None if it is not a
    # shared object. Lets the FE detect whether a shared sibling holds
    # VARIANT_IDENTIFIER to enable "include page number" in preview.
    # Direct column on PageObject — served by the existing joinedload, no extra query.
    shared_group_id: uuid.UUID | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# TextObjectConfig
# ---------------------------------------------------------------------------


class TextObjectConfigBase(BaseModel):
    text: str
    font_size: int = Field(default=12, ge=1)
    font_family: str | None = None
    bold: bool = False
    italic: bool = False


class TextObjectConfigCreate(TextObjectConfigBase):
    pass


class TextObjectConfigUpdate(BaseModel):
    text: str | None = None
    font_size: int | None = Field(default=None, ge=1)
    font_family: str | None = None
    bold: bool | None = None
    italic: bool | None = None


class TextObjectConfigRead(TextObjectConfigBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    page_object_id: uuid.UUID
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# CodeObjectConfig
# ---------------------------------------------------------------------------


class CodeObjectConfigBase(BaseModel):
    code_type: CodeType
    # Barcode symbology; must be None for QR and a valid CodeEncoding for
    # BARCODE (cross-field rule enforced in code_object_service).
    encoding: CodeEncoding | None = None
    value: str
    human_readable: bool = False
    # "Include page number" (Identification): append the page index to the
    # encoded payload. Only accepted once this object (or a linked sibling)
    # holds the VARIANT_IDENTIFIER role — enforced in code_object_service.
    include_page_index: bool = False


class CodeObjectConfigCreate(CodeObjectConfigBase):
    pass


class CodeObjectConfigUpdate(BaseModel):
    code_type: CodeType | None = None
    encoding: CodeEncoding | None = None
    value: str | None = None
    human_readable: bool | None = None
    include_page_index: bool | None = None


class CodeObjectConfigRead(CodeObjectConfigBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    page_object_id: uuid.UUID
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# FreeTextAreaConfig
# ---------------------------------------------------------------------------


class FreeTextAreaConfigBase(BaseModel):
    placeholder: str | None = None
    max_length: int | None = Field(default=None, ge=1)


class FreeTextAreaConfigCreate(FreeTextAreaConfigBase):
    pass


class FreeTextAreaConfigUpdate(BaseModel):
    placeholder: str | None = None
    max_length: int | None = Field(default=None, ge=1)


class FreeTextAreaConfigRead(FreeTextAreaConfigBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    page_object_id: uuid.UUID
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# ImageObjectConfig
#
# The file is NOT part of the create/update JSON: the object is created with
# just ``fit`` and the binary is uploaded separately (multipart) — see
# image_object_service. The read deliberately omits ``file_path`` (a
# server-internal absolute path) and exposes ``has_image`` instead.
# ---------------------------------------------------------------------------


ImageFit = Literal["contain", "cover"]


class ImageObjectConfigBase(BaseModel):
    fit: ImageFit = "contain"


class ImageObjectConfigCreate(ImageObjectConfigBase):
    pass


class ImageObjectConfigUpdate(BaseModel):
    fit: ImageFit | None = None


class ImageObjectConfigRead(ImageObjectConfigBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    page_object_id: uuid.UUID
    # File metadata — null until a file is uploaded. file_path is intentionally
    # NOT exposed (it is a server-internal storage reference).
    has_image: bool
    original_filename: str | None = None
    mime_type: str | None = None
    width_px: int | None = None
    height_px: int | None = None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Typed create requests (geometry + nested config)
# ---------------------------------------------------------------------------


class TextPageObjectCreate(PageObjectCreateBase):
    config: TextObjectConfigCreate


class CodePageObjectCreate(PageObjectCreateBase):
    config: CodeObjectConfigCreate


class FreeTextAreaPageObjectCreate(PageObjectCreateBase):
    config: FreeTextAreaConfigCreate


class ImagePageObjectCreate(PageObjectCreateBase):
    # ``config`` is optional: an IMAGE can be created with all defaults (fit =
    # contain) and have the file uploaded afterwards.
    config: ImageObjectConfigCreate = Field(default_factory=ImageObjectConfigCreate)


# ---------------------------------------------------------------------------
# Typed read models (base + nested typed config). Each pins object_type to a
# Literal so the GET-single discriminated union can resolve the right variant.
# ---------------------------------------------------------------------------


class TextPageObjectRead(PageObjectBaseRead):
    object_type: Literal[PageObjectType.TEXT] = PageObjectType.TEXT
    text_config: TextObjectConfigRead


class CodePageObjectRead(PageObjectBaseRead):
    object_type: Literal[PageObjectType.CODE] = PageObjectType.CODE
    code_config: CodeObjectConfigRead


class FreeTextAreaPageObjectRead(PageObjectBaseRead):
    object_type: Literal[PageObjectType.FREE_TEXT_AREA] = PageObjectType.FREE_TEXT_AREA
    free_text_config: FreeTextAreaConfigRead


class ImagePageObjectRead(PageObjectBaseRead):
    object_type: Literal[PageObjectType.IMAGE] = PageObjectType.IMAGE
    image_config: ImageObjectConfigRead


# ---------------------------------------------------------------------------
# Enriched page-list read
#
# QUESTION / MATRIX typed reads live in their own modules and import the base
# classes from here, so importing them at module top level would create a cycle.
# They are imported lazily below, where they are needed, and the class is rebuilt
# so Pydantic resolves the forward references.
# ---------------------------------------------------------------------------


class PageObjectWithConfig(PageObjectBaseRead):
    """Base read + every typed config (only the one matching object_type is set).

    Returned by GET /form-variant-pages/{id}/objects so the canvas can render
    content previews in a single call. Mirrors the FE ``PageObjectDetail`` 1:1;
    the page-list loader eager-loads every config (+ children) so building this
    list is free of N+1.
    """

    text_config: TextObjectConfigRead | None = None
    code_config: CodeObjectConfigRead | None = None
    free_text_config: FreeTextAreaConfigRead | None = None
    question_config: "QuestionObjectConfigRead | None" = None
    matrix_config: "MatrixObjectConfigRead | None" = None
    image_config: ImageObjectConfigRead | None = None


# Resolve the QUESTION / MATRIX forward refs without a top-level import cycle.
from app.schemas.matrix_object import MatrixObjectConfigRead  # noqa: E402
from app.schemas.question_object import QuestionObjectConfigRead  # noqa: E402

PageObjectWithConfig.model_rebuild()
