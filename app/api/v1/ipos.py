"""IPO listing, detail, history and filter-metadata endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.api.deps import CurrentUser, DbSession, IPOFilters
from app.schemas.common import ErrorResponse, Page
from app.schemas.ipo import (
    IPODetailResponse,
    IPOFilterOptions,
    IPOResponse,
    IPOSnapshotResponse,
)
from app.services.ipo import IPOService

router = APIRouter(prefix="/ipos", tags=["IPOs"])

_AUTH_RESPONSES: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse, "description": "Missing, invalid or expired token."},
    403: {"model": ErrorResponse, "description": "Account is deactivated."},
}


@router.get(
    "",
    response_model=Page[IPOResponse],
    summary="List IPOs with filtering, search, sorting and pagination",
    description=(
        "Returns a page of IPOs. All filters are composable and applied in the "
        "database.\n\n"
        "**Status** is derived per request from each IPO's dates against today "
        "in `APP_TIMEZONE`, so it is never stale: `UPCOMING`, `OPEN`, "
        "`CLOSING_TODAY`, `CLOSED`, `LISTED`.\n\n"
        "**Date filters** accept either an ISO date (`2026-09-03`) or a "
        "shortcut (`today`, `tomorrow`, `yesterday`, `this_week`, `next_week`), "
        "and independent `_from` / `_to` bounds.\n\n"
        "**Examples**\n\n"
        "- `?status=OPEN&min_gmp_percentage=15`\n"
        "- `?ipo_type=SME&exchange=NSE_SME&sort_by=gmp_percentage&sort_order=desc`\n"
        "- `?close_date=today`\n"
        "- `?close_date_from=2026-09-01&close_date_to=2026-09-07`\n"
        "- `?search=jewellers&page=1&page_size=20`"
    ),
    responses={
        200: {"description": "A page of IPOs."},
        422: {"model": ErrorResponse, "description": "Invalid filter or sort value."},
    },
)
async def list_ipos(filters: IPOFilters, session: DbSession) -> Page[IPOResponse]:
    return await IPOService(session).list_ipos(filters)


@router.get(
    "/filters",
    response_model=IPOFilterOptions,
    summary="Discover available filter values",
    description=(
        "Returns the filter values a client can offer. IPO types and exchanges "
        "are read from the live dataset, so a value that appears upstream shows "
        "up here without a deployment; statuses and sortable fields are part of "
        "the API contract.\n\n"
        "Lets a web or Flutter client build its filter UI without hardcoding "
        "backend values."
    ),
)
async def get_filter_options(session: DbSession) -> IPOFilterOptions:
    return await IPOService(session).filter_options()


@router.get(
    "/{ipo_id}",
    response_model=IPODetailResponse,
    summary="Get a single IPO",
    description=(
        "Returns full detail for one IPO, including provenance and any source "
        "fields preserved in `raw_data` that have no canonical column yet."
    ),
    responses={
        200: {"description": "The requested IPO."},
        404: {"model": ErrorResponse, "description": "IPO not found."},
    },
)
async def get_ipo(
    ipo_id: Annotated[uuid.UUID, Path(description="IPO identifier.")],
    session: DbSession,
) -> IPODetailResponse:
    return await IPOService(session).get_ipo(ipo_id)


@router.get(
    "/{ipo_id}/history",
    response_model=list[IPOSnapshotResponse],
    summary="Get an IPO's recorded changes",
    description=(
        "Returns snapshots captured whenever a tracked value changed - GMP "
        "movement over the issue's life, subscription progress, date "
        "revisions - newest first.\n\nRequires authentication."
    ),
    responses={**_AUTH_RESPONSES, 404: {"model": ErrorResponse, "description": "IPO not found."}},
)
async def get_ipo_history(
    ipo_id: Annotated[uuid.UUID, Path(description="IPO identifier.")],
    user: CurrentUser,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200, description="Maximum snapshots.")] = 50,
) -> list[IPOSnapshotResponse]:
    return await IPOService(session).get_history(ipo_id, limit=limit)
