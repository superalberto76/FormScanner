# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class PasswordChangeRequest(BaseModel):
    """Payload for POST /auth/change-password.

    Used both by the forced first-login change and any later self-service change.

    ``current_password`` is required only in the self-service case (the user is
    NOT in must_change_password); the endpoint enforces that. It is optional here
    so the forced first-login flow can keep posting just ``new_password``.
    """

    new_password: str = Field(min_length=8, max_length=128)
    current_password: str | None = None
