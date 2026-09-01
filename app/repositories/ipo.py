"""IPO persistence and querying.

Holds two responsibilities that both need to stay close to the schema:

* the **query builder** behind ``GET /api/v1/ipos`` - every filter is applied
  as SQL, so the database does the work and only one page of rows is ever
  materialised in Python;
* the **upsert** used by the scraper - existing IPOs are matched on their
  stable source identity, changed values are written in place, and a snapshot
  row is appended only when something actually moved.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, case, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.logging import get_logger
from app.db.enums import IPOStatus
from app.db.models.ipo import IPO, IPOSnapshot
from app.schemas.ipo import IPOFilterParams, IPOSortField, SortOrder
from app.services.scraper.models import NormalizedIPO, PersistenceResult
from app.utils.dates import today_in_app_timezone, utc_now

logger = get_logger(__name__)

#: Columns compared to decide whether an IPO's data has actually changed.
TRACKED_FIELDS: tuple[str, ...] = (
    "name", "ipo_type", "exchange", "source_status",
    "open_date", "close_date", "allotment_date", "listing_date",
    "price_min", "price_max", "lot_size", "issue_size_crore",
    "gmp", "gmp_percentage", "gmp_low", "gmp_high", "estimated_listing_price",
    "subscription_times", "rating", "pe_ratio", "has_anchor_investors",
)

#: Subset copied into ``ipo_snapshots`` for historical charting.
SNAPSHOT_FIELDS: tuple[str, ...] = (
    "gmp", "gmp_percentage", "subscription_times", "price_min", "price_max",
    "lot_size", "issue_size_crore", "open_date", "close_date", "listing_date",
    "source_status",
)

#: sort_by value -> column. A closed mapping: user input never reaches SQL as
#: a column name, it only ever selects a key here.
SORT_COLUMNS: dict[IPOSortField, Any] = {
    IPOSortField.NAME: IPO.name,
    IPOSortField.IPO_NAME: IPO.name,
    IPOSortField.OPEN_DATE: IPO.open_date,
    IPOSortField.CLOSE_DATE: IPO.close_date,
    IPOSortField.LISTING_DATE: IPO.listing_date,
    IPOSortField.GMP: IPO.gmp,
    IPOSortField.GMP_PERCENTAGE: IPO.gmp_percentage,
    IPOSortField.PRICE: IPO.price_max,
    IPOSortField.LOT_SIZE: IPO.lot_size,
    IPOSortField.ISSUE_SIZE: IPO.issue_size_crore,
    IPOSortField.SUBSCRIPTION: IPO.subscription_times,
    IPOSortField.CREATED_AT: IPO.created_at,
    IPOSortField.UPDATED_AT: IPO.updated_at,
}


def ipo_status_expression(today: dt.date) -> ColumnElement[str]:
    """SQL expression deriving an IPO's lifecycle stage from its dates.

    Status is computed per query rather than stored, so it can never be stale:
    an IPO that closes today becomes ``CLOSING_TODAY`` at midnight in
    ``APP_TIMEZONE`` with no job needing to run.  Branch order matters -
    "listed" outranks "closed", and "closing today" outranks "open".
    """
    today_literal = literal(today, type_=IPO.close_date.type)
    return case(
        (IPO.listing_date.is_not(None) & (IPO.listing_date <= today_literal),
         IPOStatus.LISTED.value),
        (IPO.close_date.is_not(None) & (IPO.close_date == today_literal),
         IPOStatus.CLOSING_TODAY.value),
        (IPO.close_date.is_not(None) & (IPO.close_date < today_literal),
         IPOStatus.CLOSED.value),
        (
            IPO.open_date.is_not(None)
            & (IPO.open_date <= today_literal)
            & (IPO.close_date.is_(None) | (IPO.close_date >= today_literal)),
            IPOStatus.OPEN.value,
        ),
        (IPO.open_date.is_not(None) & (IPO.open_date > today_literal),
         IPOStatus.UPCOMING.value),
        else_=IPOStatus.UNKNOWN.value,
    )


class IPORepository:
    """Data access for IPO records and their snapshots."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    async def get_by_id(self, ipo_id: uuid.UUID) -> IPO | None:
        return await self.session.get(IPO, ipo_id)

    async def list_filtered(
        self, filters: IPOFilterParams, today: dt.date | None = None
    ) -> tuple[list[IPO], int]:
        """Return one page of IPOs plus the total row count for the filters.

        The count runs as a subquery over the same predicates, so pagination
        metadata always agrees with the page contents.
        """
        today = today or today_in_app_timezone()
        statement = self._apply_filters(select(IPO), filters, today)

        count_statement = select(func.count()).select_from(statement.subquery())
        total = int((await self.session.execute(count_statement)).scalar_one())

        statement = self._apply_sorting(statement, filters)
        statement = statement.offset(filters.offset).limit(filters.page_size)
        rows = list((await self.session.execute(statement)).scalars().all())
        return rows, total

    async def list_closing_on(
        self, business_date: dt.date, min_gmp_percentage: Decimal | None = None
    ) -> list[IPO]:
        """IPOs whose close date is ``business_date`` - the notification query.

        Served by the composite ``(close_date, gmp_percentage)`` index.
        """
        statement = select(IPO).where(IPO.close_date == business_date)
        if min_gmp_percentage is not None:
            statement = statement.where(IPO.gmp_percentage >= min_gmp_percentage)
        statement = statement.order_by(IPO.gmp_percentage.desc().nullslast())
        return list((await self.session.execute(statement)).scalars().all())

    async def filter_options(self) -> dict[str, Any]:
        """Distinct classification values and numeric bounds for the UI."""
        types_result = await self.session.execute(
            select(IPO.ipo_type).distinct().order_by(IPO.ipo_type)
        )
        exchanges_result = await self.session.execute(
            select(IPO.exchange).distinct().order_by(IPO.exchange)
        )
        bounds = (
            await self.session.execute(
                select(
                    func.min(IPO.gmp_percentage),
                    func.max(IPO.gmp_percentage),
                    func.min(IPO.price_min),
                    func.max(IPO.price_max),
                )
            )
        ).one()
        return {
            "ipo_types": [value for value in types_result.scalars().all() if value],
            "exchanges": [value for value in exchanges_result.scalars().all() if value],
            "gmp_percentage_range": {"min": bounds[0], "max": bounds[1]},
            "price_range": {"min": bounds[2], "max": bounds[3]},
        }

    async def recent_snapshots(self, ipo_id: uuid.UUID, limit: int = 50) -> list[IPOSnapshot]:
        statement = (
            select(IPOSnapshot)
            .where(IPOSnapshot.ipo_id == ipo_id)
            .order_by(IPOSnapshot.captured_at.desc())
            .limit(limit)
        )
        return list((await self.session.execute(statement)).scalars().all())

    # ------------------------------------------------------------------
    # Query building
    # ------------------------------------------------------------------
    def _apply_filters(
        self, statement: Select[Any], filters: IPOFilterParams, today: dt.date
    ) -> Select[Any]:
        """Translate the validated filter model into SQL predicates.

        Adding a filter means adding one clause here - callers, routes and
        response models are untouched.
        """
        if filters.status is not None:
            statement = statement.where(
                ipo_status_expression(today) == filters.status.value
            )
        if filters.ipo_type is not None:
            statement = statement.where(IPO.ipo_type == filters.ipo_type.value)
        if filters.exchange is not None:
            statement = statement.where(IPO.exchange == filters.exchange.value)

        # Date filters: an exact/shortcut value resolves to an inclusive range,
        # and explicit *_from / *_to bounds narrow it further.
        for column, resolved, low, high in (
            (IPO.open_date, filters.resolved_open_range,
             filters.open_date_from, filters.open_date_to),
            (IPO.close_date, filters.resolved_close_range,
             filters.close_date_from, filters.close_date_to),
            (IPO.listing_date, filters.resolved_listing_range,
             filters.listing_date_from, filters.listing_date_to),
        ):
            if resolved is not None:
                statement = statement.where(column >= resolved[0], column <= resolved[1])
            if low is not None:
                statement = statement.where(column >= low)
            if high is not None:
                statement = statement.where(column <= high)

        for column, low, high in (
            (IPO.gmp, filters.min_gmp, filters.max_gmp),
            (IPO.gmp_percentage, filters.min_gmp_percentage, filters.max_gmp_percentage),
            (IPO.lot_size, filters.min_lot_size, filters.max_lot_size),
            (IPO.issue_size_crore, filters.min_issue_size, filters.max_issue_size),
            (IPO.subscription_times, filters.min_subscription, filters.max_subscription),
        ):
            if low is not None:
                statement = statement.where(column >= low)
            if high is not None:
                statement = statement.where(column <= high)

        # Price is a band: "at least 100" means the top of the band reaches 100,
        # "at most 500" means the bottom of the band stays under 500.
        if filters.min_price is not None:
            statement = statement.where(IPO.price_max >= filters.min_price)
        if filters.max_price is not None:
            statement = statement.where(IPO.price_min <= filters.max_price)

        if filters.search:
            # Escape LIKE wildcards so a literal % or _ in the query cannot
            # widen the match.
            escaped = (
                filters.search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            pattern = f"%{escaped}%"
            statement = statement.where(
                or_(
                    IPO.name.ilike(pattern, escape="\\"),
                    IPO.symbol.ilike(pattern, escape="\\"),
                )
            )
        return statement

    @staticmethod
    def _apply_sorting(statement: Select[Any], filters: IPOFilterParams) -> Select[Any]:
        """Apply the whitelisted sort, keeping NULLs last and the order total."""
        column = SORT_COLUMNS[filters.sort_by]
        ordering = (
            column.desc().nullslast()
            if filters.sort_order is SortOrder.DESC
            else column.asc().nullslast()
        )
        # id breaks ties so pagination cannot repeat or skip a row when the
        # sort column holds duplicates.
        return statement.order_by(ordering, IPO.id.asc())

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    async def upsert_many(
        self,
        records: Sequence[NormalizedIPO],
        *,
        source: str = "investorgain",
        scrape_run_id: uuid.UUID | None = None,
        write_snapshots: bool = True,
    ) -> PersistenceResult:
        """Insert new IPOs and update changed ones.

        Existing rows are loaded in a single query keyed by source identity, so
        the pass costs one SELECT plus the writes regardless of batch size.
        """
        result = PersistenceResult()
        if not records:
            return result

        identities = [record.source_ipo_id for record in records]
        existing_rows = (
            await self.session.execute(
                select(IPO).where(IPO.source == source, IPO.source_ipo_id.in_(identities))
            )
        ).scalars().all()
        existing: dict[str, IPO] = {row.source_ipo_id: row for row in existing_rows}

        now = utc_now()
        for record in records:
            current = existing.get(record.source_ipo_id)
            values = record.as_column_values()

            if current is None:
                ipo = IPO(source=source, first_seen_at=now, last_scraped_at=now,
                          data_changed_at=now, **values)
                self.session.add(ipo)
                result.inserted += 1
                continue

            changes = self._diff(current, values)
            current.last_scraped_at = now

            # raw_data is merged, not replaced, so a field the source omits on
            # one scrape is not lost from earlier ones. Merged unconditionally:
            # a newly published upstream column must be captured even when no
            # tracked field moved.
            if values.get("raw_data"):
                merged = {**(current.raw_data or {}), **values["raw_data"]}
                if merged != current.raw_data:
                    current.raw_data = merged

            if not changes:
                result.unchanged += 1
                continue

            for field_name, (_, new_value) in changes.items():
                setattr(current, field_name, new_value)
            current.data_changed_at = now
            result.updated += 1

            if write_snapshots:
                self.session.add(
                    self._build_snapshot(current, changes, scrape_run_id)
                )
                result.snapshots += 1

        await self.session.flush()
        return result

    @staticmethod
    def _diff(current: IPO, values: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
        """Return ``{field: (old, new)}`` for tracked fields that changed.

        A newly-empty value is ignored: the source intermittently blanks cells
        (an IPO with dates not yet announced), and treating that as a change
        would erase good data and spam the snapshot table.
        """
        changes: dict[str, tuple[Any, Any]] = {}
        for field_name in TRACKED_FIELDS:
            new_value = values.get(field_name)
            if new_value is None:
                continue
            old_value = getattr(current, field_name)
            if isinstance(new_value, Decimal) and isinstance(old_value, Decimal):
                if old_value == new_value:
                    continue
            elif old_value == new_value:
                continue
            changes[field_name] = (old_value, new_value)
        return changes

    @staticmethod
    def _build_snapshot(
        ipo: IPO, changes: dict[str, tuple[Any, Any]], scrape_run_id: uuid.UUID | None
    ) -> IPOSnapshot:
        return IPOSnapshot(
            ipo_id=ipo.id,
            scrape_run_id=scrape_run_id,
            **{name: getattr(ipo, name) for name in SNAPSHOT_FIELDS},
            changed_fields={
                name: {"old": _jsonable(old), "new": _jsonable(new)}
                for name, (old, new) in changes.items()
            },
        )


def _jsonable(value: Any) -> Any:
    """Render a value safely for a JSONB column."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dt.date | dt.datetime):
        return value.isoformat()
    return str(value)
