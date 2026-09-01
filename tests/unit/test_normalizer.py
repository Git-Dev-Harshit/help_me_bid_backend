"""Normalization tests against the real captured payload."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.db.enums import Exchange, IPOType
from app.services.scraper.extractor import JsonApiStrategy
from app.services.scraper.models import ExtractedRecord, RawPayload
from app.services.scraper.normalizer import IPONormalizer

REFERENCE = dt.date(2026, 9, 1)


@pytest.fixture
def normalizer() -> IPONormalizer:
    return IPONormalizer(reference_date=REFERENCE, base_url="https://www.investorgain.com")


@pytest.fixture
def live_records(api_payload_text):
    payload = RawPayload(
        url="https://example.test",
        content=api_payload_text,
        content_type="application/json",
        http_status=200,
        fetched_at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
        content_hash="x",
    )
    return JsonApiStrategy().extract(payload).records


class TestGMPCell:
    def test_splits_a_composite_gmp_cell(self, normalizer):
        """One cell carries premium, percentage and the day's low/high."""
        record = ExtractedRecord(
            fields={
                "source_ipo_id": "2081",
                "name": "Deepa Jewellers",
                "gmp": '&#8377;<b>44</b> (24.86%)<br><small><b>44 ↓ / 55 ↑</b></small>',
                "gmp_percentage": "24.86",
                "price": "177",
            }
        )
        ipo = normalizer.normalize(record)
        assert ipo.gmp == Decimal("44")
        assert ipo.gmp_percentage == Decimal("24.86")
        assert ipo.gmp_low == Decimal("44")
        assert ipo.gmp_high == Decimal("55")

    def test_negative_premium(self, normalizer):
        record = ExtractedRecord(
            fields={
                "source_ipo_id": "1",
                "name": "Alpha",
                "gmp": '&#8377;<b>-5</b> (-5.05%)<br><small><b>-5 ↓ / 7 ↑</b></small>',
            }
        )
        ipo = normalizer.normalize(record)
        assert ipo.gmp == Decimal("-5")
        assert ipo.gmp_percentage == Decimal("-5.05")

    def test_placeholder_premium_is_none_not_zero(self, normalizer):
        """'--' means unknown; recording it as 0 would be wrong."""
        record = ExtractedRecord(
            fields={
                "source_ipo_id": "1",
                "name": "Alpha",
                "gmp": "&#8377;<b>--</b> (0.00%)",
                "gmp_percentage": "0.00",
            }
        )
        ipo = normalizer.normalize(record)
        assert ipo.gmp is None
        assert ipo.gmp_percentage == Decimal("0.00")

    def test_estimated_listing_price_is_derived(self, normalizer):
        record = ExtractedRecord(
            fields={
                "source_ipo_id": "1",
                "name": "Alpha",
                "gmp": "&#8377;<b>44</b>",
                "price": "177",
            }
        )
        assert normalizer.normalize(record).estimated_listing_price == Decimal("221")


class TestClassification:
    @pytest.mark.parametrize(
        ("badge", "expected_type", "expected_exchange"),
        [
            ("IPO", IPOType.MAINBOARD, Exchange.NSE_BSE),
            ("BSE SME", IPOType.SME, Exchange.BSE_SME),
            ("NSE SME", IPOType.SME, Exchange.NSE_SME),
        ],
    )
    def test_reads_type_and_exchange_from_badges(
        self, normalizer, badge, expected_type, expected_exchange
    ):
        """The exchange is only present as a badge in the display name cell."""
        record = ExtractedRecord(
            fields={"source_ipo_id": "1", "name": "Alpha"},
            unmapped={
                "name": (
                    '<a href="/gmp/alpha-ipo/1/">Alpha</a>'
                    f'<span class="badge rounded-pill bg-secondary">{badge}</span>'
                )
            },
        )
        ipo = normalizer.normalize(record)
        assert ipo.ipo_type == expected_type.value
        assert ipo.exchange == expected_exchange.value


class TestIdentityAndName:
    def test_name_comes_from_the_link_not_the_badges(self, normalizer):
        record = ExtractedRecord(
            fields={
                "source_ipo_id": "2097",
                "name": (
                    '<a href="/gmp/fly-hi-maritime-ipo/2097/">Fly-Hi Maritime</a>'
                    '<span class="badge">BSE SME</span><span class="badge">O</span>'
                ),
            }
        )
        assert normalizer.normalize(record).name == "Fly-Hi Maritime"

    def test_identity_falls_back_to_the_url_id(self, normalizer):
        record = ExtractedRecord(
            fields={"name": "Alpha", "detail_url": "/gmp/alpha-ipo/2081/"}
        )
        assert normalizer.normalize(record).source_ipo_id == "2081"

    def test_identity_falls_back_to_a_name_slug(self, normalizer):
        record = ExtractedRecord(fields={"name": "Alpha Beta Ltd"})
        assert normalizer.normalize(record).source_ipo_id == "name:alpha-beta-ltd"

    def test_relative_urls_are_absolutised(self, normalizer):
        record = ExtractedRecord(
            fields={"source_ipo_id": "1", "name": "Alpha", "detail_url": "/gmp/alpha/1/"}
        )
        assert normalizer.normalize(record).detail_url == (
            "https://www.investorgain.com/gmp/alpha/1/"
        )

    def test_record_without_a_name_is_dropped(self, normalizer):
        assert normalizer.normalize(ExtractedRecord(fields={"source_ipo_id": "1"})) is None


class TestMiscFields:
    def test_rating_counts_emoji(self, normalizer):
        record = ExtractedRecord(
            fields={
                "source_ipo_id": "1",
                "name": "Alpha",
                "rating": "<span>&#128293;&#128293;&#128293;&#128293;</span>",
            }
        )
        assert normalizer.normalize(record).rating == 4

    @pytest.mark.parametrize(("cell", "expected"), [("✅", True), ("❌", False), ("-", None)])
    def test_anchor_flag(self, normalizer, cell, expected):
        record = ExtractedRecord(
            fields={"source_ipo_id": "1", "name": "Alpha", "anchor": cell}
        )
        assert normalizer.normalize(record).has_anchor_investors is expected

    def test_unmapped_columns_are_preserved_in_raw_data(self, normalizer):
        """A newly-added upstream column must survive, not be dropped."""
        record = ExtractedRecord(
            fields={"source_ipo_id": "1", "name": "Alpha"},
            unmapped={"brand new upstream field": "<b>some value</b>"},
        )
        raw = normalizer.normalize(record).raw_data
        assert raw["brand new upstream field"] == "some value"


class TestAgainstLiveFixture:
    def test_every_row_normalises(self, normalizer, live_records):
        normalized = normalizer.normalize_many(live_records)
        assert len(normalized) == len(live_records)

    def test_key_fields_are_typed_correctly(self, normalizer, live_records):
        normalized = normalizer.normalize_many(live_records)
        dated = [ipo for ipo in normalized if ipo.close_date]
        assert dated, "no close dates parsed from the live fixture"
        for ipo in dated:
            assert isinstance(ipo.close_date, dt.date)
            assert ipo.source_ipo_id.isdigit()
            assert ipo.ipo_type in {IPOType.MAINBOARD.value, IPOType.SME.value}

    def test_exchange_is_resolved_for_every_row(self, normalizer, live_records):
        """Regression: badges live in the display cell, not the machine name."""
        normalized = normalizer.normalize_many(live_records)
        unknown = [ipo.name for ipo in normalized if ipo.exchange == Exchange.UNKNOWN.value]
        assert not unknown, f"exchange unresolved for: {unknown}"
