"""Scraper resilience tests (spec section 27).

Each HTML variant applies one realistic upstream change.  The parser must keep
extracting from A-E, and fail *safely* - with no records and low confidence,
never a crash or a guess - on F.

Every test runs against saved fixtures; nothing here touches the network.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.db.enums import ExtractionStrategy
from app.services.scraper.extractor import (
    ExtractorChain,
    HtmlTableStrategy,
    JsonApiStrategy,
)
from app.services.scraper.field_map import map_columns, normalize_label
from app.services.scraper.models import RawPayload
from app.services.scraper.normalizer import IPONormalizer
from app.services.scraper.validator import IPOValidator, compute_confidence

REFERENCE = dt.date(2026, 9, 1)

#: Variants the parser must still read.
RESILIENT_VARIANTS = [
    "variant_a_baseline.html",
    "variant_b_renamed_classes.html",
    "variant_c_extra_wrappers.html",
    "variant_d_reordered_columns.html",
    "variant_e_missing_column.html",
]


def make_payload(content: str, *, json: bool = False) -> RawPayload:
    return RawPayload(
        url="https://example.test/report",
        content=content,
        content_type="application/json" if json else "text/html",
        http_status=200,
        fetched_at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
        content_hash="test",
    )


class TestLabelNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("~Srt_Close", "srt close"),
            ("Close Date", "close date"),
            ("CLOSE-DATE", "close date"),
            ("Price (₹)", "price"),
            ("~P/E", "p e"),
            ("GMP %", "gmp"),
        ],
    )
    def test_headers_reduce_to_comparable_tokens(self, raw, expected):
        assert normalize_label(raw) == expected


class TestColumnMapping:
    def test_maps_machine_columns_in_preference_to_display_columns(self):
        """Pre-parsed '~' columns outrank their display twins."""
        rows = [{"Close": "3-Sep", "~Srt_Close": "2026-09-03", "~ipo_name": "X", "Name": "<a>X</a>"}]
        result = map_columns(list(rows[0]), rows)
        assert result.mapping["close_date"] == "~Srt_Close"
        assert result.mapping["name"] == "~ipo_name"

    def test_identifies_a_renamed_column_by_its_values(self):
        """A header nobody recognises is still matched on value shape."""
        rows = [
            {"zz_col_9": "2026-09-03", "ipo name": "Alpha"},
            {"zz_col_9": "2026-09-04", "ipo name": "Beta"},
            {"zz_col_9": "2026-09-05", "ipo name": "Gamma"},
        ]
        result = map_columns(list(rows[0]), rows)
        matched = {m.field_name: m.method for m in result.matches}
        assert "zz_col_9" in result.mapping.values()
        assert any(method == "value_shape" for method in matched.values())

    def test_reports_missing_required_fields(self):
        rows = [{"Name": "Alpha"}]
        result = map_columns(list(rows[0]), rows)
        assert "close_date" in result.missing_required()
        assert any("required field not found" in w for w in result.warnings)


class TestJsonApiStrategy:
    def test_extracts_every_row_from_the_live_payload(self, api_payload_text, api_payload):
        result = JsonApiStrategy().extract(make_payload(api_payload_text, json=True))
        assert result.strategy is ExtractionStrategy.JSON_API
        assert len(result.records) == len(api_payload["reportTableData"])
        assert result.confidence >= 0.9

    def test_finds_rows_even_if_the_container_key_is_renamed(self, api_payload):
        """The row list is located by shape, not by the key that holds it."""
        import json

        renamed = {"msg": 1, "someNewKeyName": api_payload["reportTableData"]}
        result = JsonApiStrategy().extract(make_payload(json.dumps(renamed), json=True))
        assert len(result.records) == len(api_payload["reportTableData"])

    def test_unwraps_per_cell_value_envelopes(self):
        import json

        rows = [
            {
                "~id": {"value": "2081"},
                "~ipo_name": {"value": "Deepa"},
                "GMP": {"value": "₹44 (24.86%)"},
                "~Srt_Close": {"value": "2026-09-03"},
                "~Srt_Open": {"value": "2026-09-01"},
                "Price (₹)": {"value": "177"},
            }
        ] * 3
        result = JsonApiStrategy().extract(
            make_payload(json.dumps({"reportTableData": rows}), json=True)
        )
        assert result.records[0].get("name") == "Deepa"
        assert result.records[0].get("source_ipo_id") == "2081"

    def test_malformed_json_fails_without_raising(self):
        result = JsonApiStrategy().extract(make_payload("{not json", json=True))
        assert result.records == []
        assert result.confidence == 0.0
        assert any("not valid JSON" in w for w in result.warnings)


class TestHtmlStructureVariants:
    @pytest.mark.parametrize("variant", RESILIENT_VARIANTS)
    def test_extracts_records_from_every_survivable_variant(self, load_html, variant):
        result = HtmlTableStrategy().extract(make_payload(load_html(variant)))
        assert result.records, f"{variant}: no records extracted"
        assert result.confidence >= 0.5, f"{variant}: confidence {result.confidence}"

    @pytest.mark.parametrize("variant", RESILIENT_VARIANTS)
    def test_core_fields_survive_every_variant(self, load_html, variant):
        """Renaming, reordering and wrapping must not lose the key data."""
        result = HtmlTableStrategy().extract(make_payload(load_html(variant)))
        normalized = IPONormalizer(reference_date=REFERENCE).normalize_many(result.records)

        assert normalized, f"{variant}: nothing normalised"
        assert all(ipo.name for ipo in normalized)
        assert all(ipo.source_ipo_id for ipo in normalized)
        assert any(ipo.close_date is not None for ipo in normalized), f"{variant}: no close dates"

    def test_reordered_columns_map_to_the_same_fields(self, load_html):
        """Column order carries no meaning - only labels and values do."""
        baseline = HtmlTableStrategy().extract(make_payload(load_html("variant_a_baseline.html")))
        reordered = HtmlTableStrategy().extract(
            make_payload(load_html("variant_d_reordered_columns.html"))
        )
        assert set(baseline.field_mapping) == set(reordered.field_mapping)

    def test_missing_column_degrades_only_that_field(self, load_html):
        """Variant E drops the GMP % column; nothing else may regress.

        Compared against the baseline rather than asserted absolutely, because
        some upstream rows legitimately carry no dates yet (an IPO announced
        before its schedule is published).
        """
        normalizer = IPONormalizer(reference_date=REFERENCE)
        baseline = {
            ipo.source_ipo_id: ipo
            for ipo in normalizer.normalize_many(
                HtmlTableStrategy()
                .extract(make_payload(load_html("variant_a_baseline.html")))
                .records
            )
        }
        degraded = {
            ipo.source_ipo_id: ipo
            for ipo in normalizer.normalize_many(
                HtmlTableStrategy()
                .extract(make_payload(load_html("variant_e_missing_column.html")))
                .records
            )
        }

        assert set(degraded) == set(baseline)
        for identity, expected in baseline.items():
            assert degraded[identity].close_date == expected.close_date
            assert degraded[identity].name == expected.name
            assert degraded[identity].lot_size == expected.lot_size

        # The percentage is still recovered from the composite GMP cell, so
        # losing the dedicated column costs nothing here.
        assert any(ipo.gmp_percentage is not None for ipo in degraded.values())
        assert any(ipo.gmp is not None for ipo in degraded.values())

    def test_unrecognisable_page_fails_safely(self, load_html):
        """Variant F must produce nothing rather than inventing records."""
        result = HtmlTableStrategy().extract(
            make_payload(load_html("variant_f_unrecognisable.html"))
        )
        report = IPOValidator(REFERENCE).validate(
            IPONormalizer(reference_date=REFERENCE).normalize_many(result.records)
        )
        assert compute_confidence(result.confidence, report) == 0.0

    def test_live_client_rendered_page_yields_no_rows(self, load_html):
        """The real page ships an empty table; that must read as a failure.

        This is the behaviour that makes the JSON API the primary source.
        """
        result = HtmlTableStrategy().extract(
            make_payload(load_html("investorgain_live_page.html"))
        )
        assert result.records == []


class TestExtractorChain:
    def test_prefers_json_and_falls_back_to_html(self, api_payload_text, load_html):
        chain = ExtractorChain()
        json_result = chain.extract(make_payload(api_payload_text, json=True))
        assert json_result.strategy is ExtractionStrategy.JSON_API

        html_result = chain.extract(make_payload(load_html("variant_a_baseline.html")))
        assert html_result.strategy is ExtractionStrategy.HTML_TABLE

    def test_returns_the_best_attempt_when_nothing_clears_the_bar(self, load_html):
        result = ExtractorChain().extract(
            make_payload(load_html("variant_f_unrecognisable.html"))
        )
        assert result.records == []
        assert result.confidence < 0.5
