"""Tests for the tolerant value parsers."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.utils.parsing import (
    extract_badges,
    first_link_href,
    is_null_token,
    parse_amount_in_crore,
    parse_date,
    parse_decimal,
    parse_int,
    parse_multiplier,
    parse_percentage,
    parse_price_band,
    strip_html,
)


class TestStripHtml:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("<b>44</b>", "44"),
            # Tags collapse to a space so adjacent cells never merge into one token.
            ("&#8377;<b>44</b> (24.86%)", "₹ 44 (24.86%)"),
            ("a<br>b", "a b"),
            ("<a href='/x'>Deepa Jewellers</a>", "Deepa Jewellers"),
            ("  spaced   out  ", "spaced out"),
            (None, ""),
        ],
    )
    def test_strips_and_normalises(self, raw, expected):
        assert strip_html(raw) == expected


class TestNullTokens:
    @pytest.mark.parametrize("raw", ["", "-", "--", "N/A", "nil", "  ", "TBA"])
    def test_recognises_empty_markers(self, raw):
        assert is_null_token(raw)

    @pytest.mark.parametrize("raw", ["0", "44", "Deepa"])
    def test_real_values_are_not_null(self, raw):
        assert not is_null_token(raw)

    def test_zero_is_not_treated_as_missing(self):
        """A real zero GMP must survive; only placeholders are dropped."""
        assert parse_decimal("0") == Decimal("0")


class TestNumbers:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("44", Decimal("44")),
            ("12.5", Decimal("12.5")),
            ("-5", Decimal("-5")),
            ("1,757.00", Decimal("1757.00")),
            ("&#8377;<b>44</b>", Decimal("44")),
            ("--", None),
            ("abc", None),
        ],
    )
    def test_parse_decimal(self, raw, expected):
        assert parse_decimal(raw) == expected

    @pytest.mark.parametrize(("raw", "expected"), [("84", 84), ("1200", 1200), ("12.9", 12), ("-", None)])
    def test_parse_int(self, raw, expected):
        assert parse_int(raw) == expected


class TestPercentage:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("(24.86%)", Decimal("24.86")),
            ("24.86", Decimal("24.86")),
            ("-5.05%", Decimal("-5.05")),
            ("&#8377;<b>44</b> (24.86%)", Decimal("24.86")),
            ("0.00", Decimal("0.00")),
            ("--", None),
        ],
    )
    def test_parse_percentage(self, raw, expected):
        assert parse_percentage(raw) == expected


class TestMultiplier:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("0.88x", Decimal("0.88")), ("111.18x", Decimal("111.18")), ("1.5X", Decimal("1.5")), ("-", None)],
    )
    def test_parse_multiplier(self, raw, expected):
        assert parse_multiplier(raw) == expected


class TestAmounts:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("&#8377;459.72 Cr", Decimal("459.72")),
            ("₹1757.00 Cr", Decimal("1757.00")),
            ("250 Lakh", Decimal("2.50")),
            ("52.63", Decimal("52.63")),
            ("", None),
        ],
    )
    def test_parse_amount_in_crore(self, raw, expected):
        assert parse_amount_in_crore(raw) == expected


class TestPriceBand:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("177", (Decimal("177"), Decimal("177"))),
            ("100-110", (Decimal("100"), Decimal("110"))),
            ("100 to 110", (Decimal("100"), Decimal("110"))),
            ("110-100", (Decimal("100"), Decimal("110"))),
            ("", (None, None)),
        ],
    )
    def test_parse_price_band(self, raw, expected):
        assert parse_price_band(raw) == expected


class TestDates:
    REFERENCE = dt.date(2026, 9, 1)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2026-09-03", dt.date(2026, 9, 3)),
            ("3-Sep", dt.date(2026, 9, 3)),
            ("03-Sep-2026", dt.date(2026, 9, 3)),
            ("3 Sep 2026", dt.date(2026, 9, 3)),
            ("Sep 3, 2026", dt.date(2026, 9, 3)),
            ("03/09/2026", dt.date(2026, 9, 3)),
            ("", None),
            ("--", None),
            ("not a date", None),
        ],
    )
    def test_parse_date(self, raw, expected):
        assert parse_date(raw, self.REFERENCE) == expected

    def test_year_inference_picks_the_nearest_year(self):
        """A bare '1-Jan' read in late December belongs to *next* year."""
        december = dt.date(2026, 12, 28)
        assert parse_date("1-Jan", december) == dt.date(2027, 1, 1)

    def test_year_inference_looks_backwards_too(self):
        january = dt.date(2026, 1, 3)
        assert parse_date("28-Dec", january) == dt.date(2025, 12, 28)

    def test_impossible_date_returns_none(self):
        assert parse_date("31-Feb-2026", self.REFERENCE) is None


class TestHtmlHelpers:
    def test_extract_badges(self):
        html = (
            '<a href="/gmp/x/1/">Fly-Hi</a> '
            '<span class="badge rounded-pill bg-secondary">BSE SME</span>'
            '<span class="badge rounded-pill bg-success">O</span>'
        )
        assert extract_badges(html) == ["BSE SME", "O"]

    def test_first_link_href(self):
        assert first_link_href('<a href="/gmp/deepa/2081/">Deepa</a>') == "/gmp/deepa/2081/"
        assert first_link_href("no link here") is None
