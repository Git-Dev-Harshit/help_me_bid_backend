"""Validation and confidence-scoring tests."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.services.scraper.models import NormalizedIPO, ValidationReport
from app.services.scraper.validator import IPOValidator, compute_confidence

REFERENCE = dt.date(2026, 9, 1)


def make_ipo(**overrides) -> NormalizedIPO:
    values: dict = {
        "source_ipo_id": "2081",
        "name": "Deepa Jewellers",
        "open_date": dt.date(2026, 9, 1),
        "close_date": dt.date(2026, 9, 3),
        "gmp_percentage": Decimal("24.86"),
        "price_min": Decimal("177"),
        "price_max": Decimal("177"),
    }
    values.update(overrides)
    return NormalizedIPO(**values)


class TestFatalRejections:
    def test_accepts_a_sound_record(self):
        report = IPOValidator(REFERENCE).validate([make_ipo()])
        assert len(report.valid) == 1
        assert report.validity_ratio == 1.0

    def test_rejects_a_record_with_no_name(self):
        report = IPOValidator(REFERENCE).validate([make_ipo(name="")])
        assert len(report.rejected) == 1

    def test_rejects_inverted_open_close_dates(self):
        """Reversed dates mean the columns were swapped - the row is unusable."""
        report = IPOValidator(REFERENCE).validate(
            [make_ipo(open_date=dt.date(2026, 9, 5), close_date=dt.date(2026, 9, 1))]
        )
        assert len(report.rejected) == 1

    def test_rejects_duplicate_identities_within_one_batch(self):
        report = IPOValidator(REFERENCE).validate([make_ipo(), make_ipo()])
        assert len(report.valid) == 1
        assert len(report.rejected) == 1


class TestFieldLevelRepair:
    def test_out_of_range_values_are_cleared_not_fatal(self):
        """One bad cell must not cost us an otherwise good IPO."""
        report = IPOValidator(REFERENCE).validate([make_ipo(gmp_percentage=Decimal("99999"))])
        assert len(report.valid) == 1
        assert report.valid[0].gmp_percentage is None

    def test_inverted_price_band_is_swapped(self):
        report = IPOValidator(REFERENCE).validate(
            [make_ipo(price_min=Decimal("200"), price_max=Decimal("100"))]
        )
        ipo = report.valid[0]
        assert (ipo.price_min, ipo.price_max) == (Decimal("100"), Decimal("200"))

    def test_wildly_drifted_dates_are_cleared(self):
        report = IPOValidator(REFERENCE).validate(
            [make_ipo(listing_date=dt.date(2005, 1, 1))]
        )
        assert report.valid[0].listing_date is None

    def test_impossible_lot_size_is_cleared(self):
        report = IPOValidator(REFERENCE).validate([make_ipo(lot_size=-5)])
        assert report.valid[0].lot_size is None


class TestConfidence:
    def test_empty_result_scores_zero(self):
        assert compute_confidence(1.0, ValidationReport()) == 0.0

    def test_perfect_extraction_scores_high(self):
        report = IPOValidator(REFERENCE).validate([make_ipo(), make_ipo(source_ipo_id="2")])
        assert compute_confidence(1.0, report) >= 0.9

    def test_poor_column_mapping_drags_the_score_down(self):
        """Rows can be valid while the structure is still unrecognised."""
        report = IPOValidator(REFERENCE).validate([make_ipo(), make_ipo(source_ipo_id="2")])
        assert compute_confidence(0.2, report) < 0.6

    def test_mixed_validity_lowers_the_score(self):
        report = IPOValidator(REFERENCE).validate(
            [make_ipo(), make_ipo(source_ipo_id="2", name=""), make_ipo(source_ipo_id="3")]
        )
        assert 0.0 < compute_confidence(1.0, report) < 1.0
