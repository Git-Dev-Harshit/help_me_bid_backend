"""Current-user endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.repositories.user import UserRepository
from app.schemas.common import ErrorResponse
from app.schemas.user import UserResponse, UserUpdateRequest

router = APIRouter(prefix="/users", tags=["Users"])

_AUTH_RESPONSES: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse, "description": "Missing, invalid or expired token."},
    403: {"model": ErrorResponse, "description": "Account is deactivated."},
}


@router.get(
    "/me",
    response_model=UserResponse,
    summary="Get the authenticated user's profile",
    description="Returns the account associated with the supplied access token.",
    responses=_AUTH_RESPONSES,
)
async def get_me(user: CurrentUser) -> UserResponse:
    return UserResponse.model_validate(user)


@router.patch(
    "/me",
    response_model=UserResponse,
    summary="Update the authenticated user's profile",
    description=(
        "Updates the optional profile fields. Omitted fields are left "
        "unchanged. The phone number is the login identity and cannot be "
        "changed here."
    ),
    responses={**_AUTH_RESPONSES, 409: {"model": ErrorResponse, "description": "Email in use."}},
)
async def update_me(
    payload: UserUpdateRequest, user: CurrentUser, session: DbSession
) -> UserResponse:
    repository = UserRepository(session)
    updated = await repository.update_profile(
        user,
        name=payload.name,
        email=str(payload.email) if payload.email else None,
    )
    await session.commit()
    return UserResponse.model_validate(updated)
