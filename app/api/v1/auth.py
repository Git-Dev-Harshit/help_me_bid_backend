"""Authentication endpoints."""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import DbSession
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse
from app.schemas.common import ErrorResponse
from app.schemas.user import UserResponse
from app.services.auth import AuthService

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new account",
    description=(
        "Creates an account. The phone number is mandatory and is normalised to "
        "E.164 before storage, so `9876543210`, `+91 98765 43210` and "
        "`098765 43210` all refer to the same account.\n\n"
        "Passwords must be at least 8 characters and contain a letter and a "
        "digit. They are hashed with Argon2id and never stored or logged in "
        "plaintext."
    ),
    responses={
        201: {"description": "Account created."},
        409: {"model": ErrorResponse, "description": "Phone number already registered."},
        422: {"model": ErrorResponse, "description": "Invalid phone number or weak password."},
        429: {"model": ErrorResponse, "description": "Too many registration attempts."},
    },
)
async def register(payload: RegisterRequest, session: DbSession) -> UserResponse:
    user = await AuthService(session).register(payload)
    return UserResponse.model_validate(user)


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Log in and obtain an access token",
    description=(
        "Exchanges a phone number and password for a JWT access token.\n\n"
        "Send the token on subsequent requests as "
        "`Authorization: Bearer <access_token>`. Failures are reported "
        "identically whether the account is unknown or the password is wrong, "
        "so this endpoint cannot be used to discover registered numbers."
    ),
    responses={
        200: {"description": "Authentication succeeded."},
        401: {"model": ErrorResponse, "description": "Incorrect phone number or password."},
        403: {"model": ErrorResponse, "description": "Account is deactivated."},
        429: {"model": ErrorResponse, "description": "Too many login attempts."},
    },
)
async def login(payload: LoginRequest, session: DbSession) -> TokenResponse:
    service = AuthService(session)
    user = await service.authenticate(payload)
    return service.issue_token(user)
