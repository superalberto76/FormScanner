# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.core.security import PASSWORD_MIN_LENGTH
from app.models.enums import UITheme, UserRole


class UserBase(BaseModel):
    email: EmailStr
    full_name: str | None = None
    role: UserRole = UserRole.USER
    disabled: bool = False


class UserCreate(UserBase):
    # Plaintext password; the service layer is responsible for hashing.
    password: str


class UserCreateByAdmin(BaseModel):
    """Payload for POST /users — an admin creates a non-admin user.

    Deliberately has NO ``role`` field: this endpoint always creates a USER. The
    new account starts with ``must_change_password=True`` (forced first-login
    change). ``full_name`` is not collected; first/last name are separate.
    """

    first_name: str = Field(min_length=1, max_length=255)
    last_name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    # TEMPORARY password: min length ONLY (no rich policy). This is the SAME rule
    # as the admin reset (AdminPasswordReset below) — the password is provisional
    # and the rich policy (digit/upper/lower/special) is deferred to the forced
    # first-login change. Do NOT add the rich policy here.
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=128)


class AdminPasswordReset(BaseModel):
    """Payload for POST /users/{id}/reset-password — admin sets a TEMP password.

    Same rule as ``UserCreateByAdmin.password``: min length ONLY, NO rich policy.
    The reset forces ``must_change_password=True``, so the rich policy applies at
    the user's next login (the unchanged ``POST /auth/change-password`` path) —
    NOT here. This must never route through ``auth_service.change_password``.
    """

    new_password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=128)


class UserDisableUpdate(BaseModel):
    """Payload for PATCH /users/{id} — flip the disabled flag.

    Deliberately minimal: ONLY ``disabled``. NOT a reuse of the admin
    ``UserUpdate`` (which also carries role/full_name) — this endpoint changes
    access, nothing else.
    """

    disabled: bool


class UserUpdate(BaseModel):
    full_name: str | None = None
    role: UserRole | None = None
    disabled: bool | None = None


class UserProfileUpdate(BaseModel):
    """Self-service profile edit. first/last only — NEVER role/disabled/email.

    Deliberately SEPARATE from the admin ``UserUpdate`` above: a self-service user
    may rename themselves but can never reach ``role``/``disabled``/``email``. Those
    fields are unreachable by construction — extra keys in the request body are
    ignored by Pydantic, so they cannot be smuggled in.
    """

    first_name: str | None = Field(default=None, min_length=1, max_length=255)
    last_name: str | None = Field(default=None, min_length=1, max_length=255)
    # UI theme preference. The ONLY non-name field a self-service user
    # may set here; role/disabled/email remain unreachable by construction. The
    # existing PATCH /auth/me loop applies it via model_dump(exclude_unset=True).
    ui_theme: UITheme | None = None


class UserSearchItem(BaseModel):
    """A single hit from GET /users/search.

    Mirrors the project-member item shape so the FE can render the same Avatar +
    name UI. ``has_avatar`` comes from the ``User.has_avatar`` property; the
    internal ``avatar_path`` is never exposed.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    first_name: str | None = None
    last_name: str | None = None
    email: EmailStr
    has_avatar: bool = False


class UserListItem(BaseModel):
    """A single row in the admin user list — GET /users.

    Carries just what the management table renders (name / email / role / status)
    plus the two fields the FE needs to decide which rows get actions
    (``role``/``disabled``). ``has_avatar`` mirrors the ``User.has_avatar``
    property so the row can show the avatar; the internal ``avatar_path`` is never
    exposed (same rule as ``UserRead``/``UserSearchItem``).
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    first_name: str | None = None
    last_name: str | None = None
    role: UserRole
    disabled: bool
    has_avatar: bool = False


class PaginatedUsers(BaseModel):
    """Envelope for GET /users — one page plus the totals the FE needs to page.

    ``total`` is the full count of matching rows (not just this page) so the UI
    can render "X–Y of N" and enable/disable Prev/Next.
    """

    items: list[UserListItem]
    total: int
    limit: int
    offset: int


class UserRead(UserBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    first_name: str | None = None
    last_name: str | None = None
    # Drives the FE forced first-login password change.
    must_change_password: bool = False
    # Persisted UI theme mode; default SYSTEM for safety so an older
    # row without the column still deserializes coherently.
    ui_theme: UITheme = UITheme.SYSTEM
    # Whether a profile photo has been uploaded. Populated
    # from the ``User.has_avatar`` property via from_attributes — the internal
    # ``avatar_path`` is intentionally NEVER exposed (mirrors has_image).
    has_avatar: bool = False
    created_at: datetime
    updated_at: datetime
    last_login_at: datetime | None = None
