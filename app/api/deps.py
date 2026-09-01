"""Shared FastAPI dependencies: database sessions and the current user."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import InvalidTokenError
from app.core.security import extract_user_id
from app.db.models.user import User
from app.db.session import get_db
from app.schemas.common import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from app.schemas.ipo import IPOFilterParams
from app.services.auth import AuthService

# auto_error=False so a missing header raises our own error envelope rather
# than FastAPI's default {"detail": ...} shape.
bearer_scheme = HTTPBearer(
    auto_error=False,
    description="JWT access token issued by POST /api/v1/auth/login.",
)

DbSession = Annotated[AsyncSession, Depends(get_db)]


async def get_current_user(
    session: DbSession,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ] = None,
) -> User:
    """Resolve the authenticated user from the ``Authorization`` header."""
    if credentials is None or not credentials.credentials:
        raise InvalidTokenError("An access token is required.")
    user_id = extract_user_id(credentials.credentials)
    return await AuthService(session).get_active_user(user_id)


CurrentUser = Annotated[User, Depends(get_current_user)]


def ipo_filter_params(
    filters: Annotated[IPOFilterParams, Query()],
) -> IPOFilterParams:
    """Bind and validate the IPO listing query string.

    FastAPI expands the model's fields into individual, documented query
    parameters, so the whole filter surface stays declarative.
    """
    return filters


IPOFilters = Annotated[IPOFilterParams, Depends(ipo_filter_params)]


def pagination_params(
    page: Annotated[int, Query(ge=1, description="1-based page number.")] = 1,
    page_size: Annotated[
        int,
        Query(ge=1, le=MAX_PAGE_SIZE, description=f"Items per page (max {MAX_PAGE_SIZE})."),
    ] = DEFAULT_PAGE_SIZE,
) -> tuple[int, int]:
    """Simple pagination for endpoints that need no filtering."""
    return page, page_size


Pagination = Annotated[tuple[int, int], Depends(pagination_params)]
