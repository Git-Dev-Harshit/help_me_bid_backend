"""Integration tests for IPO persistence, filtering, sorting and pagination."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.repositories.ipo import IPORepository
from app.services.ipo import derive_status
from app.services.scraper.models import NormalizedIPO
from app.utils.dates import today_in_app_timezone

pytestmark = pytest.mark.integration

# The business date in APP_TIMEZONE, not the container's UTC date. They differ
# for 5.5 hours every day (IST is UTC+5:30), and the application derives status
# and notification eligibility from the business date - so fixtures built from
# dt.date.today() would make the suite fail nightly.
TODAY = today_in_app_timezone()


def sample(**overrides) -> NormalizedIPO:
    values: dict = {
        "source_ipo_id": "1001",
        "name": "Alpha Industries",
        "ipo_type": "MAINBOARD",
        "exchange": "NSE_BSE",
        "open_date": TODAY - dt.timedelta(days=1),
        "close_date": TODAY + dt.timedelta(days=1),
        "listing_date": TODAY + dt.timedelta(days=6),
        "price_min": Decimal("100"),
        "price_max": Decimal("110"),
        "lot_size": 100,
        "issue_size_crore": Decimal("250.00"),
        "gmp": Decimal("25"),
        "gmp_percentage": Decimal("22.73"),
        "subscription_times": Decimal("3.5"),
    }
    values.update(overrides)
    return NormalizedIPO(**values)


async def seed(session, records):
    result = await IPORepository(session).upsert_many(records)
    await session.commit()
    return result


class TestUpsert:
    async def test_inserts_new_records(self, db_session):
        result = await seed(db_session, [sample(), sample(source_ipo_id="1002", name="Beta")])
        assert (result.inserted, result.updated, result.unchanged) == (2, 0, 0)

    async def test_rerunning_with_identical_data_changes_nothing(self, db_session):
        """The scraper runs every 30 minutes; unchanged data must be a no-op."""
        await seed(db_session, [sample()])
        result = await seed(db_session, [sample()])
        assert (result.inserted, result.updated, result.unchanged) == (0, 0, 1)
        assert result.snapshots == 0

    async def test_never_duplicates_an_ipo_identity(self, db_session):
        for _ in range(3):
            await seed(db_session, [sample()])
        from sqlalchemy import func, select

        from app.db.models.ipo import IPO

        total = (await db_session.execute(select(func.count()).select_from(IPO))).scalar_one()
        assert total == 1

    async def test_gmp_change_updates_in_place_and_records_a_snapshot(self, db_session):
        await seed(db_session, [sample()])
        result = await seed(db_session, [sample(gmp=Decimal("40"), gmp_percentage=Decimal("36.4"))])
        assert (result.inserted, result.updated) == (0, 1)
        assert result.snapshots == 1

        snapshots = await IPORepository(db_session).recent_snapshots(
            (await IPORepository(db_session).list_filtered(_filters()))[0][0].id
        )
        assert snapshots
        assert "gmp" in snapshots[0].changed_fields
        assert snapshots[0].changed_fields["gmp"]["new"] == 40.0

    async def test_a_newly_blank_upstream_value_does_not_erase_good_data(self, db_session):
        """The source intermittently blanks cells; that must not wipe the row."""
        await seed(db_session, [sample()])
        await seed(db_session, [sample(gmp=None, close_date=None)])

        rows, _ = await IPORepository(db_session).list_filtered(_filters())
        assert rows[0].gmp == Decimal("25")
        assert rows[0].close_date is not None

    async def test_unmapped_source_fields_are_merged_not_replaced(self, db_session):
        await seed(db_session, [sample(raw_data={"first": "a"})])
        await seed(db_session, [sample(raw_data={"second": "b"})])
        rows, _ = await IPORepository(db_session).list_filtered(_filters())
        assert rows[0].raw_data == {"first": "a", "second": "b"}


def _filters(**overrides):
    from app.schemas.ipo import IPOFilterParams

    return IPOFilterParams(**overrides)


class TestFiltering:
    @pytest.fixture(autouse=True)
    async def _seed(self, db_session):
        await seed(
            db_session,
            [
                sample(source_ipo_id="1", name="Alpha Mainboard", ipo_type="MAINBOARD",
                       exchange="NSE_BSE", gmp_percentage=Decimal("22.73"),
                       close_date=TODAY + dt.timedelta(days=1)),
                sample(source_ipo_id="2", name="Beta SME", ipo_type="SME",
                       exchange="NSE_SME", gmp_percentage=Decimal("40.00"),
                       close_date=TODAY, price_min=Decimal("50"), price_max=Decimal("50")),
                sample(source_ipo_id="3", name="Gamma SME", ipo_type="SME",
                       exchange="BSE_SME", gmp_percentage=Decimal("5.00"),
                       open_date=TODAY - dt.timedelta(days=7),
                       close_date=TODAY - dt.timedelta(days=5),
                       listing_date=TODAY - dt.timedelta(days=1)),
                sample(source_ipo_id="4", name="Delta Upcoming", ipo_type="MAINBOARD",
                       exchange="NSE_BSE", gmp_percentage=Decimal("15.00"),
                       open_date=TODAY + dt.timedelta(days=3),
                       close_date=TODAY + dt.timedelta(days=5),
                       listing_date=TODAY + dt.timedelta(days=10)),
            ],
        )

    async def test_no_filters_returns_everything(self, db_session):
        _, total = await IPORepository(db_session).list_filtered(_filters())
        assert total == 4

    @pytest.mark.parametrize(
        ("status", "expected"),
        [("OPEN", 1), ("CLOSING_TODAY", 1), ("LISTED", 1), ("UPCOMING", 1)],
    )
    async def test_status_is_derived_from_dates(self, db_session, status, expected):
        _, total = await IPORepository(db_session).list_filtered(_filters(status=status))
        assert total == expected

    async def test_sql_and_python_status_derivation_agree(self, db_session):
        """The filter expression and the response field must never diverge."""
        rows, _ = await IPORepository(db_session).list_filtered(_filters(page_size=100))
        for row in rows:
            expected = derive_status(row, TODAY)
            _, matching = await IPORepository(db_session).list_filtered(
                _filters(status=expected.value, page_size=100)
            )
            assert matching >= 1

    async def test_ipo_type_filter(self, db_session):
        _, total = await IPORepository(db_session).list_filtered(_filters(ipo_type="SME"))
        assert total == 2

    async def test_exchange_filter(self, db_session):
        _, total = await IPORepository(db_session).list_filtered(_filters(exchange="NSE_SME"))
        assert total == 1

    async def test_gmp_percentage_range(self, db_session):
        _, total = await IPORepository(db_session).list_filtered(
            _filters(min_gmp_percentage=Decimal("15"))
        )
        assert total == 3
        _, bounded = await IPORepository(db_session).list_filtered(
            _filters(min_gmp_percentage=Decimal("15"), max_gmp_percentage=Decimal("25"))
        )
        assert bounded == 2

    async def test_close_date_shortcut_today(self, db_session):
        _, total = await IPORepository(db_session).list_filtered(_filters(close_date="today"))
        assert total == 1

    async def test_close_date_explicit_range(self, db_session):
        _, total = await IPORepository(db_session).list_filtered(
            _filters(
                close_date_from=TODAY,
                close_date_to=TODAY + dt.timedelta(days=2),
            )
        )
        assert total == 2

    async def test_price_band_overlap(self, db_session):
        _, total = await IPORepository(db_session).list_filtered(
            _filters(min_price=Decimal("100"))
        )
        assert total == 3

    async def test_search_is_case_insensitive_substring(self, db_session):
        for term in ("beta", "BETA", "eta S"):
            _, total = await IPORepository(db_session).list_filtered(_filters(search=term))
            assert total == 1, term

    async def test_search_wildcards_are_escaped(self, db_session):
        """A literal % must not turn into a match-everything pattern."""
        _, total = await IPORepository(db_session).list_filtered(_filters(search="%"))
        assert total == 0

    async def test_filters_compose(self, db_session):
        _, total = await IPORepository(db_session).list_filtered(
            _filters(ipo_type="SME", min_gmp_percentage=Decimal("10"), close_date="today")
        )
        assert total == 1


class TestSortingAndPagination:
    @pytest.fixture(autouse=True)
    async def _seed(self, db_session):
        await seed(
            db_session,
            [
                sample(source_ipo_id=str(i), name=f"IPO {i:02d}",
                       gmp_percentage=Decimal(str(i * 5)))
                for i in range(1, 8)
            ],
        )

    async def test_sort_descending(self, db_session):
        rows, _ = await IPORepository(db_session).list_filtered(
            _filters(sort_by="gmp_percentage", sort_order="desc", page_size=100)
        )
        values = [row.gmp_percentage for row in rows]
        assert values == sorted(values, reverse=True)

    async def test_sort_ascending(self, db_session):
        rows, _ = await IPORepository(db_session).list_filtered(
            _filters(sort_by="gmp_percentage", sort_order="asc", page_size=100)
        )
        values = [row.gmp_percentage for row in rows]
        assert values == sorted(values)

    async def test_pages_do_not_overlap_or_skip(self, db_session):
        repository = IPORepository(db_session)
        page1, total = await repository.list_filtered(_filters(page=1, page_size=3))
        page2, _ = await repository.list_filtered(_filters(page=2, page_size=3))
        page3, _ = await repository.list_filtered(_filters(page=3, page_size=3))

        ids = [row.id for row in page1 + page2 + page3]
        assert total == 7
        assert len(ids) == 7
        assert len(set(ids)) == 7

    async def test_pagination_metadata(self, api_client, db_session):
        response = await api_client.get("/api/v1/ipos?page=1&page_size=3")
        assert response.status_code == 200
        meta = response.json()["pagination"]
        assert meta["page"] == 1
        assert meta["page_size"] == 3
        assert meta["total_pages"] == meta["total_items"] // 3 + (
            1 if meta["total_items"] % 3 else 0
        )
        assert meta["has_previous"] is False

    async def test_page_size_is_capped(self, api_client):
        assert (await api_client.get("/api/v1/ipos?page_size=1000")).status_code == 422

    async def test_unknown_sort_field_is_rejected(self, api_client):
        """sort_by reaches ORDER BY, so it must be validated, never interpolated."""
        response = await api_client.get("/api/v1/ipos?sort_by=name;DROP TABLE ipos")
        assert response.status_code == 422
