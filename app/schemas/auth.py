"""Registration, login and token payloads."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field

PhoneNumber = Annotated[
    str,
    Field(
        min_length=6,
        max_length=20,
        description=(
            "Phone number in any common format. Normalised to E.164 before "
            "storage, so '9876543210', '+91 98765 43210' and '098765 43210' "
            "all resolve to the same account (default region: DEFAULT_PHONE_REGION)."
        ),
        examples=["+919876543210"],
    ),
]

Password = Annotated[
    str,
    Field(
        min_length=8,
        max_length=128,
        description="Must contain at least one letter and one digit.",
        examples=["Str0ngPassw0rd"],
    ),
]


class RegisterRequest(BaseModel):
    """New account payload. Phone number is mandatory; name and email are not."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "phone_number": "+919876543210",
                "password": "Str0ngPassw0rd",
                "name": "Asha Menon",
                "email": "asha@example.com",
            }
        }
    )

    phone_number: PhoneNumber
    password: Password
    name: str | None = Field(default=None, max_length=120, examples=["Asha Menon"])
    email: EmailStr | None = Field(default=None, examples=["asha@example.com"])


class LoginRequest(BaseModel):
    """Credentials for the login endpoint."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"phone_number": "+919876543210", "password": "Str0ngPassw0rd"}
        }
    )

    phone_number: PhoneNumber
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    """Issued access token.

    ``expires_in`` is seconds from issue. Send the token as
    ``Authorization: Bearer <access_token>``.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
                "token_type": "bearer",
                "expires_in": 1800,
            }
        }
    )

    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Token lifetime in seconds.", examples=[1800])
