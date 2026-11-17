# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.enums import FormProjectStatus, FormVariantStatus


# ---------------------------------------------------------------------------
# FormVariantPage
# ---------------------------------------------------------------------------


class FormVariantPageBase(BaseModel):
    page_index: int
    image_path: str | None = None
    width_px: int | None = None
    height_px: int | None = None
    dpi: float | None = None


class FormVariantPageCreate(FormVariantPageBase):
    # form_variant_id comes from the URL path param.
    pass


class FormVariantPageRead(FormVariantPageBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    form_variant_id: uuid.UUID
    created_at: datetime


class FormVariantPageReorder(BaseModel):
    """Body of PATCH /form-variants/{id}/pages/reorder.

    A full permutation of the variant's current page ids, in the desired order.
    The service validates it is exactly that set before renumbering 1..n.
    """

    ordered_page_ids: list[uuid.UUID]


# ---------------------------------------------------------------------------
# FormVariant
# ---------------------------------------------------------------------------


class FormVariantBase(BaseModel):
    name: str
    description: str | None = None


class FormVariantCreate(FormVariantBase):
    # form_project_id comes from the URL path param; created_by_user_id from auth.
    # root_key is optional; the service generates a stable default when omitted.
    root_key: str | None = None


class FormVariantUpdate(BaseModel):
    # Only name/description are editable, and only while the variant is DRAFT.
    # Status transitions use the dedicated /publish endpoint.
    name: str | None = None
    description: str | None = None


class FormVariantRead(FormVariantBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    form_project_id: uuid.UUID
    root_key: str
    status: FormVariantStatus
    version_number: int
    original_file_path: str | None = None
    original_filename: str | None = None
    published_at: datetime | None = None
    created_by_user_id: uuid.UUID
    updated_by_user_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class FormVariantListItem(BaseModel):
    """Flat summary row returned by GET /form-projects/{id}/variants."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    form_project_id: uuid.UUID
    root_key: str
    name: str
    description: str | None = None
    version_number: int
    status: FormVariantStatus
    published_at: datetime | None = None
    page_count: int
    created_at: datetime
    updated_at: datetime


class FormVariantDetail(FormVariantRead):
    """Full variant view with its pages embedded."""

    pages: list[FormVariantPageRead] = []
    # The parent project's lifecycle status. The None default exists ONLY so
    # model_validate(variant) (which has no such attribute) doesn't fail — it is
    # ALWAYS populated at runtime in get_variant_detail (mirrors how `pages` is
    # defaulted [] then set). The FE reads it to lock editing on a frozen
    # project.
    form_project_status: FormProjectStatus | None = None


class VariantMaxScoreRead(BaseModel):
    """GET /form-variants/{id}/max-score.

    The maximum score a sheet of this variant can earn
    (``scoring_rules.variant_max_score``), computed on the fly, in any status.
    """

    max_score: float
