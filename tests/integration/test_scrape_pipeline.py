"""Integration tests for the scrape pipeline's persistence and failure policy.

The critical behaviour verified here: when the upstream source changes shape,
existing IPO data is left untouched and the failure becomes observable.
No test contacts the network - the HTTP client is replaced with a stub that
replays saved fixtures.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.db.enums import ScrapeStatus
from app.repositories.ipo import IPORepository
from app.repositories.scrape import ScrapeRepository
from app.schemas.ipo import IPOFilterParams
from app.services.scraper.models import NormalizedIPO, RawPayload
from app.services.scraper.pipeline import ScrapePipeline
from app.utils.dates import today_in_app_timezone

pytestmark = pytest.mark.integration


class StubHTTPClient:
    """Replays a canned payload instead of making a request."""

    def __init__(self, content: str, content_type: str = "application/json") -> None:
        self.content = content
        self.content_type = content_type
        self.calls: list[str] = []

    async def __aenter__(self) -> StubHTTPClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def fetch(self, url: str) -> RawPayload:
        self.calls.append(url)
        return RawPayload(
            url=url,
            content=self.content,
            content_type=self.content_type,
            http_status=200,
            fetched_at=dt.datetime.now(dt.UTC),
            content_hash="stub",
        )


def build_pipeline(session, client) -> ScrapePipeline:
    return ScrapePipeline(
        ipo_repository=IPORepository(session),
        scrape_repository=ScrapeRepository(session),
        http_client=client,
    )


class TestSuccessfulScrape:
    async def test_persists_every_valid_record(self, db_session, api_payload_text, api_payload):
        pipeline = build_pipeline(db_session, StubHTTPClient(api_payload_text))
        outcome = await pipeline.run()
        await db_session.commit()

        expected = len(api_payload["reportTableData"])
        assert outcome.status in {ScrapeStatus.SUCCESS.value, ScrapeStatus.PARTIAL.value}
        assert outcome.records_found == expected
        assert outcome.persistence.inserted == expected
        assert outcome.confidence >= 0.9

    async def test_second_run_updates_rather_than_duplicating(
        self, db_session, api_payload_text, api_payload
    ):
        client = StubHTTPClient(api_payload_text)
        await build_pipeline(db_session, client).run()
        await db_session.commit()
        outcome = await build_pipeline(db_session, client).run()
        await db_session.commit()

        assert outcome.persistence.inserted == 0
        _, total = await IPORepository(db_session).list_filtered(IPOFilterParams(page_size=100))
        assert total == len(api_payload["reportTableData"])

    async def test_records_a_run_row(self, db_session, api_payload_text):
        await build_pipeline(db_session, StubHTTPClient(api_payload_text)).run()
        await db_session.commit()

        runs = await ScrapeRepository(db_session).latest_runs()
        assert runs
        assert runs[0].records_found > 0
        assert runs[0].duration_ms is not None
        assert runs[0].field_mapping


class TestHtmlFallback:
    async def test_falls_back_to_html_when_json_is_unusable(self, db_session, load_html):
        """A broken JSON feed must not stop the scrape."""
        pipeline = build_pipeline(
            db_session,
            _SequencedClient(
                [("{}", "application/json"),
                 (load_html("variant_a_baseline.html"), "text/html")]
            ),
        )
        outcome = await pipeline.run()
        await db_session.commit()

        assert outcome.persistence.inserted > 0
        assert any("used HTML page" in w for w in outcome.warnings)


class TestFailurePolicy:
    async def test_unrecognisable_page_does_not_touch_existing_data(
        self, db_session, load_html
    ):
        """The headline safety property: bad scrape, no data loss."""
        await IPORepository(db_session).upsert_many(
            [
                NormalizedIPO(
                    source_ipo_id="9999",
                    name="Existing IPO",
                    gmp=Decimal("50"),
                    gmp_percentage=Decimal("30"),
                    close_date=today_in_app_timezone(),
                )
            ]
        )
        await db_session.commit()

        outcome = await build_pipeline(
            db_session,
            StubHTTPClient(load_html("variant_f_unrecognisable.html"), "text/html"),
        ).run()
        await db_session.commit()

        assert outcome.status == ScrapeStatus.FAILED.value
        assert outcome.error_code in {
            "SCRAPER_EXTRACTION_FAILED",
            "SCRAPER_LOW_CONFIDENCE",
            "SCRAPER_NO_VALID_RECORDS",
        }
        assert outcome.persistence.inserted == 0
        assert outcome.persistence.updated == 0

        rows, total = await IPORepository(db_session).list_filtered(IPOFilterParams())
        assert total == 1
        assert rows[0].name == "Existing IPO"
        assert rows[0].gmp == Decimal("50")

    async def test_failure_retains_the_raw_payload_for_debugging(
        self, db_session, load_html
    ):
        await build_pipeline(
            db_session,
            StubHTTPClient(load_html("variant_f_unrecognisable.html"), "text/html"),
        ).run()
        await db_session.commit()

        from sqlalchemy import select

        from app.db.models.scrape import ScrapeRawPayload

        payloads = (await db_session.execute(select(ScrapeRawPayload))).scalars().all()
        assert payloads, "raw payload was not retained for a failed run"
        assert payloads[0].byte_size > 0

    async def test_failure_is_observable_in_the_run_record(self, db_session, load_html):
        await build_pipeline(
            db_session,
            StubHTTPClient(load_html("variant_f_unrecognisable.html"), "text/html"),
        ).run()
        await db_session.commit()

        run = (await ScrapeRepository(db_session).latest_runs())[0]
        assert run.status == ScrapeStatus.FAILED.value
        assert run.error_code
        assert run.error_message

    async def test_structure_change_is_reported_as_a_warning(
        self, db_session, api_payload_text, load_html
    ):
        """A field that stops mapping is flagged before it becomes a failure."""
        await build_pipeline(db_session, StubHTTPClient(api_payload_text)).run()
        await db_session.commit()

        outcome = await build_pipeline(
            db_session,
            StubHTTPClient(load_html("variant_e_missing_column.html"), "text/html"),
        ).run()
        await db_session.commit()

        assert any("structure change" in warning for warning in outcome.warnings)


class _SequencedClient:
    """Returns a different payload per call, to exercise the fallback path."""

    def __init__(self, payloads: list[tuple[str, str]]) -> None:
        self.payloads = payloads
        self.index = 0

    async def __aenter__(self) -> _SequencedClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def fetch(self, url: str) -> RawPayload:
        content, content_type = self.payloads[min(self.index, len(self.payloads) - 1)]
        self.index += 1
        return RawPayload(
            url=url,
            content=content,
            content_type=content_type,
            http_status=200,
            fetched_at=dt.datetime.now(dt.UTC),
            content_hash=f"stub-{self.index}",
        )
