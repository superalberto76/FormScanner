# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.enums import MembershipRole


class GroupBase(BaseModel):
    name: str
    description: str | None = None


class GroupCreate(GroupBase):
    pass


class GroupUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class GroupRead(GroupBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# GroupMembership
# ---------------------------------------------------------------------------


class GroupMembershipCreate(BaseModel):
    user_id: uuid.UUID
    membership_role: MembershipRole = MembershipRole.MEMBER


class GroupMembershipUpdate(BaseModel):
    membership_role: MembershipRole


class GroupMembershipRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    group_id: uuid.UUID
    user_id: uuid.UUID
    membership_role: MembershipRole
    created_at: datetime
