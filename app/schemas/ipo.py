"""IPO response models and the listing filter/sort/pagination query model."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from app.db.enums import Exchange, IPOStatus, IPOType
from app.schemas.common import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from app.utils.dates import today_in_app_timezone


class IPOSortField(StrEnum):
    """Columns the listing endpoint may be sorted by.

    A closed enum, not a free string: the value reaches an ``ORDER BY`` clause,
    so it is validated against this whitelist and mapped to a column object -
    never interpolated into SQL.
    """

    NAME = "name"
    IPO_NAME = "ipo_name"  # alias for `name`
    OPEN_DATE = "open_date"
    CLOSE_DATE = "close_date"
    LISTING_DATE = "listing_date"
    GMP = "gmp"
    GMP_PERCENTAGE = "gmp_percentage"
    PRICE = "price"
    LOT_SIZE = "lot_size"
    ISSUE_SIZE = "issue_size"
    SUBSCRIPTION = "subscription"
    CREATED_AT = "created_at"
    UPDATED_AT = "updated_at"


class SortOrder(StrEnum):
    ASC = "asc"
    DESC = "desc"


class DateShortcut(StrEnum):
    """Relative date windows resolved against the business timezone."""

    TODAY = "today"
    TOMORROW = "tomorrow"
    YESTERDAY = "yesterday"
    THIS_WEEK = "this_week"
    NEXT_WEEK = "next_week"


def resolve_date_shortcut(value: str, today: dt.date | None = None) -> tuple[dt.date, dt.date]:
    """Expand a shortcut into an inclusive ``(from, to)`` date range.

    Weeks run Monday-Sunday, matching Indian market convention.
    """
    today = today or today_in_app_timezone()
    shortcut = DateShortcut(value)
    if shortcut is DateShortcut.TODAY:
        return today, today
    if shortcut is DateShortcut.TOMORROW:
        day = today + dt.timedelta(days=1)
        return day, day
    if shortcut is DateShortcut.YESTERDAY:
        day = today - dt.timedelta(days=1)
        return day, day
    monday = today - dt.timedelta(days=today.weekday())
    if shortcut is DateShortcut.THIS_WEEK:
        return monday, monday + dt.timedelta(days=6)
    next_monday = monday + dt.timedelta(days=7)
    return next_monday, next_monday + dt.timedelta(days=6)


def _parse_date_field(value: str | dt.date | None) -> tuple[dt.date, dt.date] | None:
    """Accept an ISO date or a shortcut keyword, returning an inclusive range."""
    if value is None:
        return None
    if isinstance(value, dt.date):
        return value, value
    text = str(value).strip().lower()
    if not text:
        return None
    if text in set(DateShortcut):
        return resolve_date_shortcut(text)
    try:
        parsed = dt.date.fromisoformat(text)
    except ValueError as exc:
        allowed = ", ".join(sorted(set(DateShortcut)))
        raise ValueError(
            f"Expected an ISO date (YYYY-MM-DD) or one of: {allowed}"
        ) from exc
    return parsed, parsed


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------
class PriceBand(BaseModel):
    """Issue price band. ``min`` equals ``max`` for a fixed-price issue."""

    min: Decimal | None = Field(default=None, examples=[100])
    max: Decimal | None = Field(default=None, examples=[110])


class GMPBand(BaseModel):
    """Observed grey-market premium range for the day."""

    low: Decimal | None = Field(default=None, examples=[44])
    high: Decimal | None = Field(default=None, examples=[55])


class IPOResponse(BaseModel):
    """An IPO as returned by the listing and detail endpoints.

    Carries everything a list row or detail screen needs, so a client never has
    to issue a second request per IPO.  ``status`` is computed per request from
    the IPO's dates against today in ``APP_TIMEZONE`` and is therefore never
    stale.
    """

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "id": "0f1d2c3b-4a59-4c8e-9b0a-1d2e3f4a5b6c",
                "name": "Deepa Jewellers",
                "symbol": None,
                "ipo_type": "MAINBOARD",
                "exchange": "NSE_BSE",
                "status": "OPEN",
                "open_date": "2026-09-01",
                "close_date": "2026-09-03",
                "allotment_date": "2026-09-04",
                "listing_date": "2026-09-08",
                "price": {"min": 177, "max": 177},
                "lot_size": 84,
                "issue_size_crore": 459.72,
                "gmp": 44,
                "gmp_percentage": 24.86,
                "gmp_band": {"low": 44, "high": 55},
                "estimated_listing_price": 221,
                "subscription_times": 0.88,
                "rating": 4,
                "pe_ratio": None,
                "has_anchor_investors": True,
                "detail_url": "https://www.investorgain.com/gmp/deepa-jewellers-ipo/2081/",
                "updated_at": "2026-09-01T10:30:00Z",
            }
        },
    )

    id: uuid.UUID
    name: str
    symbol: str | None = None
    ipo_type: IPOType
    exchange: Exchange
    status: IPOStatus = Field(description="Derived from the IPO's dates and today's date.")

    open_date: dt.date | None = None
    close_date: dt.date | None = None
    allotment_date: dt.date | None = None
    listing_date: dt.date | None = None

    price: PriceBand
    lot_size: int | None = None
    issue_size_crore: Decimal | None = Field(
        default=None, description="Total issue size in crore (INR)."
    )

    gmp: Decimal | None = Field(default=None, description="Latest grey-market premium (INR).")
    gmp_percentage: Decimal | None = Field(
        default=None, description="GMP as a percentage of the cap price."
    )
    gmp_band: GMPBand
    estimated_listing_price: Decimal | None = None

    subscription_times: Decimal | None = Field(
        default=None, description="Overall subscription multiple, e.g. 0.88 for 0.88x."
    )
    rating: int | None = Field(default=None, description="Source rating, 1-5.")
    pe_ratio: Decimal | None = None
    has_anchor_investors: bool | None = None

    detail_url: str | None = None
    updated_at: dt.datetime


class IPODetailResponse(IPOResponse):
    """Detail view: the list payload plus provenance and any extra source fields."""

    source: str = Field(description="Upstream data source identifier.")
    source_status: str | None = Field(default=None, description="Raw upstream status code.")
    raw_data: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Source fields with no canonical column yet, preserved verbatim so "
            "newly published data is never dropped."
        ),
    )
    first_seen_at: dt.datetime
    last_scraped_at: dt.datetime | None = None
    data_changed_at: dt.datetime | None = None


class IPOSnapshotResponse(BaseModel):
    """A historical observation of an IPO's volatile values."""

    model_config = ConfigDict(from_attributes=True)

    captured_at: dt.datetime
    gmp: Decimal | None = None
    gmp_percentage: Decimal | None = None
    subscription_times: Decimal | None = None
    changed_fields: dict[str, Any] = Field(default_factory=dict)


class IPOFilterOptions(BaseModel):
    """Filter values a client can offer, discovered from the live dataset."""

    ipo_types: list[str] = Field(description="IPO types currently present in the data.")
    exchanges: list[str] = Field(description="Exchanges currently present in the data.")
    statuses: list[str] = Field(description="Every status the API can return.")
    sort_fields: list[str] = Field(description="Accepted values for sort_by.")
    gmp_percentage_range: dict[str, Decimal | None] = Field(
        description="Observed min/max GMP percentage, for slider bounds."
    )
    price_range: dict[str, Decimal | None] = Field(description="Observed min/max issue price.")


# ---------------------------------------------------------------------------
# Listing query
# ---------------------------------------------------------------------------
class IPOFilterParams(BaseModel):
    """Every filter, sort and pagination option for ``GET /api/v1/ipos``.

    Used as a FastAPI dependency so the route stays declarative and the
    repository receives one validated object.  Adding a filter means adding a
    field here plus a clause in the query builder - nothing else changes.
    """

    model_config = ConfigDict(extra="forbid")

    # --- Classification ---------------------------------------------------
    status: IPOStatus | None = Field(default=None, description="Derived lifecycle stage.")
    ipo_type: IPOType | None = Field(default=None, description="MAINBOARD or SME.")
    exchange: Exchange | None = Field(default=None, description="Listing exchange.")

    # --- Dates ------------------------------------------------------------
    open_date: str | None = Field(
        default=None, description="ISO date or shortcut (today, tomorrow, this_week, next_week)."
    )
    open_date_from: dt.date | None = None
    open_date_to: dt.date | None = None
    close_date: str | None = Field(
        default=None, description="ISO date or shortcut (today, tomorrow, this_week, next_week)."
    )
    close_date_from: dt.date | None = None
    close_date_to: dt.date | None = None
    listing_date: str | None = Field(default=None, description="ISO date or shortcut.")
    listing_date_from: dt.date | None = None
    listing_date_to: dt.date | None = None

    # --- Numeric ranges ---------------------------------------------------
    min_gmp: Decimal | None = None
    max_gmp: Decimal | None = None
    min_gmp_percentage: Decimal | None = None
    max_gmp_percentage: Decimal | None = None
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    min_lot_size: Annotated[int | None, Field(ge=0)] = None
    max_lot_size: Annotated[int | None, Field(ge=0)] = None
    min_issue_size: Decimal | None = Field(default=None, description="Issue size in crore.")
    max_issue_size: Decimal | None = Field(default=None, description="Issue size in crore.")
    min_subscription: Decimal | None = None
    max_subscription: Decimal | None = None

    # --- Search / sort / paging -------------------------------------------
    search: str | None = Field(
        default=None,
        max_length=100,
        description="Case-insensitive substring match on IPO name and symbol.",
    )
    sort_by: IPOSortField = Field(
        default=IPOSortField.CLOSE_DATE, description="Field to sort by."
    )
    sort_order: SortOrder = Field(default=SortOrder.DESC, description="Sort direction.")
    page: Annotated[int, Field(ge=1)] = 1
    page_size: Annotated[int, Field(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE

    # Resolved once by the validator below. Private attributes, not fields:
    # a model field here would be published as a query parameter of its own.
    _resolved_open_range: tuple[dt.date, dt.date] | None = PrivateAttr(default=None)
    _resolved_close_range: tuple[dt.date, dt.date] | None = PrivateAttr(default=None)
    _resolved_listing_range: tuple[dt.date, dt.date] | None = PrivateAttr(default=None)

    @property
    def resolved_open_range(self) -> tuple[dt.date, dt.date] | None:
        return self._resolved_open_range

    @property
    def resolved_close_range(self) -> tuple[dt.date, dt.date] | None:
        return self._resolved_close_range

    @property
    def resolved_listing_range(self) -> tuple[dt.date, dt.date] | None:
        return self._resolved_listing_range

    @model_validator(mode="after")
    def _resolve_and_check(self) -> Self:
        self._resolved_open_range = _parse_date_field(self.open_date)
        self._resolved_close_range = _parse_date_field(self.close_date)
        self._resolved_listing_range = _parse_date_field(self.listing_date)

        for low, high, label in (
            (self.min_gmp, self.max_gmp, "gmp"),
            (self.min_gmp_percentage, self.max_gmp_percentage, "gmp_percentage"),
            (self.min_price, self.max_price, "price"),
            (self.min_lot_size, self.max_lot_size, "lot_size"),
            (self.min_issue_size, self.max_issue_size, "issue_size"),
            (self.min_subscription, self.max_subscription, "subscription"),
        ):
            if low is not None and high is not None and low > high:
                raise ValueError(f"min_{label} must not exceed max_{label}")

        for low, high, label in (
            (self.open_date_from, self.open_date_to, "open_date"),
            (self.close_date_from, self.close_date_to, "close_date"),
            (self.listing_date_from, self.listing_date_to, "listing_date"),
        ):
            if low is not None and high is not None and low > high:
                raise ValueError(f"{label}_from must not be after {label}_to")

        if self.search is not None:
            self.search = self.search.strip() or None
        return self

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size
