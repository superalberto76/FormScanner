# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.enums import FormProjectStatus, FormProjectType, ProjectRole


class FormProjectBase(BaseModel):
    type: FormProjectType
    name: str
    description: str | None = None
    event_date: datetime | None = None


class FormProjectCreate(FormProjectBase):
    # owner_id is derived from the authenticated user at the service layer.
    pass


class FormProjectUpdate(BaseModel):
    # type is intentionally not editable post-creation (it drives variant semantics).
    # status is intentionally NOT here: lifecycle changes go ONLY through the
    # dedicated POST /form-projects/{id}/transition endpoint (owner-only, graph-
    # validated). This closes the former free-text status backdoor.
    name: str | None = None
    description: str | None = None
    event_date: datetime | None = None


class FormProjectRead(FormProjectBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    owner_id: uuid.UUID
    status: FormProjectStatus
    # The CALLER's role on THIS project is OWNER (same meaning as in
    # FormProjectListItem). Defaults False so model_validate(orm_row) — which has
    # no such attribute — succeeds; the detail endpoint sets it from the caller's
    # membership. Drives owner-only lifecycle controls on the FE.
    is_owner: bool = False
    created_at: datetime
    updated_at: datetime


class FormProjectMemberRead(BaseModel):
    """A single member row for GET /form-projects/{id}/members."""

    user_id: uuid.UUID
    first_name: str | None = None
    last_name: str | None = None
    role: ProjectRole
    has_avatar: bool


class FormProjectListItem(BaseModel):
    """Flat summary row returned by GET /form-projects (FE-friendly)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: FormProjectType
    name: str
    description: str | None = None
    event_date: datetime | None = None
    status: FormProjectStatus
    owner_id: uuid.UUID
    is_owner: bool
    variant_count: int
    # Avatar-stack seam: the first ≤3 members (owners first)
    # plus the full count, so the list renders the stack with NO extra calls.
    members_preview: list[FormProjectMemberRead] = []
    member_count: int = 0
    created_at: datetime
    updated_at: datetime


class FormProjectTransition(BaseModel):
    """Body for POST /form-projects/{id}/transition — request a lifecycle move.

    The service validates the move against the fixed transition graph (owner-only)
    and applies any side effects (e.g. the archive cascade).
    """

    target_status: FormProjectStatus


class ScanReadiness(BaseModel):
    """Whether a project's PUBLISHED variants are coherently configured for a
    scan batch.

    Read-only diagnostic returned by GET /form-projects/{id}/scan-readiness —
    ALWAYS 200 (never a 409): ``ready=False`` carries a human-readable ``reason``
    instead of an error. Batch creation and uploads enforce the same rule and
    refuse with the structured 409 ``NOT_SCAN_READY`` carrying that ``reason``.
    The rule mirrors the scanning matcher's need for a VARIANT_IDENTIFIER
    when several variants share the print run:
      * 0 published variants   → not ready (nothing to scan);
      * exactly 1              → always ready (no matching ambiguity);
      * >1                     → ready only if one carries a VARIANT_IDENTIFIER,
                                 whose id is surfaced in
                                 ``variant_identifier_variant_id``.

    Checked ahead of all of the above: several PUBLISHED variants sharing ONE
    VARIANT_IDENTIFIER value → not ready, with the variants involved listed in
    ``colliding_variant_ids``. ``publish_variant`` now refuses to create such a
    collision, so this only fires on data that predates that gate.
    """

    ready: bool
    reason: str | None = None
    published_variant_count: int
    variant_identifier_variant_id: uuid.UUID | None = None
    # Additive (default empty): the PUBLISHED variants that share a
    # VARIANT_IDENTIFIER value with at least one sibling, sorted, deduplicated
    # across all colliding values. Empty in every other readiness state, so
    # existing consumers that ignore the field are unaffected.
    colliding_variant_ids: list[uuid.UUID] = []


class MemberAddRequest(BaseModel):
    """Body for POST /form-projects/{id}/members — add an existing user."""

    user_id: uuid.UUID


class MemberRoleUpdate(BaseModel):
    """Body for PATCH /form-projects/{id}/members/{user_id} — change a role."""

    role: ProjectRole
