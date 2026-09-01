"""User-facing account schemas."""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserResponse(BaseModel):
    """Public view of an account. Never includes the password hash."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    phone_number: str = Field(description="Normalised E.164 phone number.")
    name: str | None = None
    email: EmailStr | None = None
    is_active: bool
    created_at: dt.datetime
    last_login_at: dt.datetime | None = None


class UserUpdateRequest(BaseModel):
    """Editable profile fields. Omitted fields are left unchanged."""

    name: str | None = Field(default=None, max_length=120)
    email: EmailStr | None = None
