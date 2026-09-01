"""Validation and confidence scoring for normalized IPO records.

Two independent judgements are made here:

*Per record* - is this row internally coherent enough to persist?  Rows that
fail a **fatal** check (no identity, no name, impossible date ordering) are
rejected individually; the rest of the batch still goes through.

*Per run* - taken together, do these records look like the IPO dataset at all?
That is the confidence score.  It blends how many columns the extractor
recognised with how many rows survived validation, and falls below
``SCRAPER_MIN_CONFIDENCE`` when the upstream structure has changed enough that
persisting would risk corrupting good data.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.core.logging import get_logger
from app.services.scraper.models import (
    NormalizedIPO,
    ValidationIssue,
    ValidationReport,
)

logger = get_logger(__name__)

# Plausibility bounds. Generous on purpose: these catch parser mistakes
# (a percentage read as a price, a lot size read from the wrong column),
# not unusual-but-real market values.
MAX_REASONABLE_PRICE = Decimal("100000")
MAX_REASONABLE_GMP_PERCENTAGE = Decimal("1000")
MIN_REASONABLE_GMP_PERCENTAGE = Decimal("-100")
MAX_REASONABLE_ISSUE_SIZE_CRORE = Decimal("500000")
MAX_REASONABLE_LOT_SIZE = 1_000_000
MAX_REASONABLE_SUBSCRIPTION = Decimal("100000")
# Dates far outside this window indicate a misparsed year.
MAX_DATE_DRIFT_DAYS = 800


class IPOValidator:
    """Validate normalized records and score the batch's credibility."""

    def __init__(self, reference_date: dt.date | None = None) -> None:
        self.reference_date = reference_date or dt.date.today()

    def validate(self, records: list[NormalizedIPO]) -> ValidationReport:
        report = ValidationReport()
        seen_ids: set[str] = set()

        for index, record in enumerate(records):
            issues = self._validate_record(index, record)
            fatal = [issue for issue in issues if issue.fatal]

            # Guard against an extractor that duplicated rows; keep the first.
            if record.source_ipo_id in seen_ids:
                fatal.append(
                    ValidationIssue(
                        index, "source_ipo_id",
                        f"duplicate identity {record.source_ipo_id!r} in the same batch",
                        fatal=True,
                    )
                )
            seen_ids.add(record.source_ipo_id)

            report.issues.extend(issues)
            report.issues.extend(i for i in fatal if i not in issues)
            if fatal:
                report.rejected.append(record)
            else:
                report.valid.append(record)
        return report

    # ------------------------------------------------------------------
    def _validate_record(self, index: int, record: NormalizedIPO) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []

        def fail(field: str, message: str, *, fatal: bool = False) -> None:
            issues.append(ValidationIssue(index, field, message, fatal=fatal))

        # --- Identity (fatal) --------------------------------------------
        if not record.source_ipo_id:
            fail("source_ipo_id", "missing identity", fatal=True)
        if not record.name or len(record.name.strip()) < 2:
            fail("name", "missing or implausibly short IPO name", fatal=True)

        # --- Dates --------------------------------------------------------
        if (
            record.open_date
            and record.close_date
            and record.open_date > record.close_date
        ):
            # Ordering this wrong means the columns were swapped; the whole row
            # is untrustworthy.
            fail("close_date", "close_date precedes open_date", fatal=True)

        if (
            record.close_date
            and record.listing_date
            and record.listing_date < record.close_date
        ):
            fail("listing_date", "listing_date precedes close_date")

        for field_name in ("open_date", "close_date", "allotment_date", "listing_date"):
            value: dt.date | None = getattr(record, field_name)
            if value and abs((value - self.reference_date).days) > MAX_DATE_DRIFT_DAYS:
                fail(field_name, f"date {value} is implausibly far from today")
                setattr(record, field_name, None)

        # --- Numeric plausibility ----------------------------------------
        # Out-of-range numbers are cleared rather than rejecting the row: a
        # single bad cell should not cost us an otherwise good IPO record.
        if record.price_min is not None and not (
            Decimal(0) <= record.price_min <= MAX_REASONABLE_PRICE
        ):
            fail("price_min", f"price {record.price_min} out of range")
            record.price_min = None
        if record.price_max is not None and not (
            Decimal(0) <= record.price_max <= MAX_REASONABLE_PRICE
        ):
            fail("price_max", f"price {record.price_max} out of range")
            record.price_max = None
        if (
            record.price_min is not None
            and record.price_max is not None
            and record.price_min > record.price_max
        ):
            record.price_min, record.price_max = record.price_max, record.price_min
            fail("price_min", "price band was inverted; values swapped")

        if record.gmp_percentage is not None and not (
            MIN_REASONABLE_GMP_PERCENTAGE
            <= record.gmp_percentage
            <= MAX_REASONABLE_GMP_PERCENTAGE
        ):
            fail("gmp_percentage", f"gmp percentage {record.gmp_percentage} out of range")
            record.gmp_percentage = None

        if record.issue_size_crore is not None and not (
            Decimal(0) <= record.issue_size_crore <= MAX_REASONABLE_ISSUE_SIZE_CRORE
        ):
            fail("issue_size_crore", f"issue size {record.issue_size_crore} out of range")
            record.issue_size_crore = None

        if record.lot_size is not None and not (0 < record.lot_size <= MAX_REASONABLE_LOT_SIZE):
            fail("lot_size", f"lot size {record.lot_size} out of range")
            record.lot_size = None

        if record.subscription_times is not None and not (
            Decimal(0) <= record.subscription_times <= MAX_REASONABLE_SUBSCRIPTION
        ):
            fail("subscription_times", f"subscription {record.subscription_times} out of range")
            record.subscription_times = None

        if record.rating is not None and not (0 <= record.rating <= 5):
            record.rating = None

        return issues


def compute_confidence(
    extraction_confidence: float,
    report: ValidationReport,
    expected_minimum_records: int = 1,
) -> float:
    """Combine structural and row-level signals into one 0.0-1.0 score.

    * ``extraction_confidence`` answers "did we recognise the *columns*?"
    * ``validity_ratio`` answers "did the *rows* make sense once parsed?"

    Both must hold for the run to be trustworthy, so they are averaged rather
    than maximised, and an empty result is scored zero regardless of how well
    the columns matched.
    """
    if report.total < expected_minimum_records or not report.valid:
        return 0.0
    score = (extraction_confidence * 0.6) + (report.validity_ratio * 0.4)
    return round(min(max(score, 0.0), 1.0), 3)
