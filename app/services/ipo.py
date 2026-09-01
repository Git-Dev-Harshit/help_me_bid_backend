"""IPO read services: querying, status derivation and response assembly."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import IPONotFoundError
from app.db.enums import Exchange, IPOStatus, IPOType
from app.db.models.ipo import IPO
from app.repositories.ipo import IPORepository
from app.schemas.common import Page, PaginationMeta
from app.schemas.ipo import (
    GMPBand,
    IPODetailResponse,
    IPOFilterOptions,
    IPOFilterParams,
    IPOResponse,
    IPOSnapshotResponse,
    IPOSortField,
    PriceBand,
)
from app.utils.dates import today_in_app_timezone


def derive_status(ipo: IPO, today: dt.date) -> IPOStatus:
    """Python mirror of :func:`app.repositories.ipo.ipo_status_expression`.

    The SQL expression filters; this one labels the rows that come back.  The
    two must agree, which is asserted by the test suite.
    """
    if ipo.listing_date is not None and ipo.listing_date <= today:
        return IPOStatus.LISTED
    if ipo.close_date is not None and ipo.close_date == today:
        return IPOStatus.CLOSING_TODAY
    if ipo.close_date is not None and ipo.close_date < today:
        return IPOStatus.CLOSED
    if (
        ipo.open_date is not None
        and ipo.open_date <= today
        and (ipo.close_date is None or ipo.close_date >= today)
    ):
        return IPOStatus.OPEN
    if ipo.open_date is not None and ipo.open_date > today:
        return IPOStatus.UPCOMING
    return IPOStatus.UNKNOWN


def _coerce_enum[T](enum_cls: type[T], value: str | None, fallback: T) -> T:
    """Map a stored string onto an enum, tolerating values added upstream."""
    if not value:
        return fallback
    try:
        return enum_cls(value)  # type: ignore[call-arg]
    except ValueError:
        return fallback


def to_response(ipo: IPO, today: dt.date) -> IPOResponse:
    """Project an ORM row onto the public list/detail shape."""
    return IPOResponse(
        id=ipo.id,
        name=ipo.name,
        symbol=ipo.symbol,
        ipo_type=_coerce_enum(IPOType, ipo.ipo_type, IPOType.UNKNOWN),
        exchange=_coerce_enum(Exchange, ipo.exchange, Exchange.UNKNOWN),
        status=derive_status(ipo, today),
        open_date=ipo.open_date,
        close_date=ipo.close_date,
        allotment_date=ipo.allotment_date,
        listing_date=ipo.listing_date,
        price=PriceBand(min=ipo.price_min, max=ipo.price_max),
        lot_size=ipo.lot_size,
        issue_size_crore=ipo.issue_size_crore,
        gmp=ipo.gmp,
        gmp_percentage=ipo.gmp_percentage,
        gmp_band=GMPBand(low=ipo.gmp_low, high=ipo.gmp_high),
        estimated_listing_price=ipo.estimated_listing_price,
        subscription_times=ipo.subscription_times,
        rating=ipo.rating,
        pe_ratio=ipo.pe_ratio,
        has_anchor_investors=ipo.has_anchor_investors,
        detail_url=ipo.detail_url,
        updated_at=ipo.updated_at,
    )


def to_detail_response(ipo: IPO, today: dt.date) -> IPODetailResponse:
    """Detail projection: the list fields plus provenance."""
    return IPODetailResponse(
        **to_response(ipo, today).model_dump(),
        source=ipo.source,
        source_status=ipo.source_status,
        raw_data=ipo.raw_data or {},
        first_seen_at=ipo.first_seen_at,
        last_scraped_at=ipo.last_scraped_at,
        data_changed_at=ipo.data_changed_at,
    )


class IPOService:
    """Application logic for the IPO endpoints."""

    def __init__(self, session: AsyncSession) -> None:
        self.repository = IPORepository(session)

    async def list_ipos(self, filters: IPOFilterParams) -> Page[IPOResponse]:
        """Return a filtered, sorted, paginated page of IPOs."""
        today = today_in_app_timezone()
        rows, total = await self.repository.list_filtered(filters, today)
        return Page[IPOResponse](
            items=[to_response(row, today) for row in rows],
            pagination=PaginationMeta.build(
                page=filters.page, page_size=filters.page_size, total_items=total
            ),
        )

    async def get_ipo(self, ipo_id: uuid.UUID) -> IPODetailResponse:
        ipo = await self.repository.get_by_id(ipo_id)
        if ipo is None:
            raise IPONotFoundError()
        return to_detail_response(ipo, today_in_app_timezone())

    async def get_history(self, ipo_id: uuid.UUID, limit: int = 50) -> list[IPOSnapshotResponse]:
        """Recorded changes for one IPO, newest first."""
        ipo = await self.repository.get_by_id(ipo_id)
        if ipo is None:
            raise IPONotFoundError()
        snapshots = await self.repository.recent_snapshots(ipo_id, limit=limit)
        return [IPOSnapshotResponse.model_validate(snapshot) for snapshot in snapshots]

    async def filter_options(self) -> IPOFilterOptions:
        """Values a client can present in filter controls.

        Classification values come from the live data (so a newly-appearing
        exchange shows up without a deploy); statuses and sort fields are
        contract-defined and therefore enumerated from the code.
        """
        options = await self.repository.filter_options()
        return IPOFilterOptions(
            ipo_types=options["ipo_types"],
            exchanges=options["exchanges"],
            statuses=[status.value for status in IPOStatus],
            sort_fields=[field.value for field in IPOSortField],
            gmp_percentage_range=options["gmp_percentage_range"],
            price_range=options["price_range"],
        )
