"""Value objects passed between scraper stages.

The pipeline is deliberately linear and each stage hands the next a plain,
immutable object:

    fetch -> RawPayload -> extract -> ExtractionResult
          -> normalize -> NormalizedIPO[] -> validate -> ValidationReport
          -> persist -> PersistenceResult

Keeping these as dataclasses (rather than ORM models or dicts) means every
stage is unit-testable in isolation, with no database and no network.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.db.enums import ExtractionStrategy


@dataclass(frozen=True, slots=True)
class RawPayload:
    """An untouched upstream response, kept verbatim for replay and debugging."""

    url: str
    content: str
    content_type: str
    http_status: int
    fetched_at: dt.datetime
    content_hash: str

    @property
    def byte_size(self) -> int:
        return len(self.content.encode("utf-8"))

    @property
    def is_json(self) -> bool:
        return "json" in self.content_type.lower()


@dataclass(slots=True)
class ExtractedRecord:
    """One IPO row as it came out of an extractor: canonical field -> raw cell.

    Values are still whatever the source provided (HTML fragments, strings).
    Type conversion is the normalizer's job, so an extractor never has to know
    how a value should be interpreted.
    """

    fields: dict[str, Any] = field(default_factory=dict)
    unmapped: dict[str, Any] = field(default_factory=dict)

    def get(self, name: str) -> Any:
        return self.fields.get(name)


@dataclass(slots=True)
class ExtractionResult:
    """Everything one extraction strategy produced, plus how well it went."""

    strategy: ExtractionStrategy
    records: list[ExtractedRecord] = field(default_factory=list)
    # canonical field name -> source column that supplied it
    field_mapping: dict[str, str] = field(default_factory=dict)
    # Source columns that matched no canonical field (candidates for promotion).
    unmapped_columns: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # 0.0-1.0 confidence that this really is the IPO dataset.
    confidence: float = 0.0

    @property
    def succeeded(self) -> bool:
        return bool(self.records)


@dataclass(slots=True)
class NormalizedIPO:
    """A canonical, typed IPO ready for validation and persistence."""

    source_ipo_id: str
    name: str

    ipo_type: str = "UNKNOWN"
    exchange: str = "UNKNOWN"
    source_status: str | None = None
    symbol: str | None = None
    slug: str | None = None
    detail_url: str | None = None

    open_date: dt.date | None = None
    close_date: dt.date | None = None
    allotment_date: dt.date | None = None
    listing_date: dt.date | None = None

    price_min: Decimal | None = None
    price_max: Decimal | None = None
    lot_size: int | None = None
    issue_size_crore: Decimal | None = None

    gmp: Decimal | None = None
    gmp_percentage: Decimal | None = None
    gmp_low: Decimal | None = None
    gmp_high: Decimal | None = None
    estimated_listing_price: Decimal | None = None

    subscription_times: Decimal | None = None
    rating: int | None = None
    pe_ratio: Decimal | None = None
    has_anchor_investors: bool | None = None

    source_updated_text: str | None = None
    raw_data: dict[str, Any] = field(default_factory=dict)

    def as_column_values(self) -> dict[str, Any]:
        """Project onto the ``ipos`` table's column names."""
        return {
            "source_ipo_id": self.source_ipo_id,
            "name": self.name,
            "symbol": self.symbol,
            "slug": self.slug,
            "detail_url": self.detail_url,
            "ipo_type": self.ipo_type,
            "exchange": self.exchange,
            "source_status": self.source_status,
            "open_date": self.open_date,
            "close_date": self.close_date,
            "allotment_date": self.allotment_date,
            "listing_date": self.listing_date,
            "price_min": self.price_min,
            "price_max": self.price_max,
            "lot_size": self.lot_size,
            "issue_size_crore": self.issue_size_crore,
            "gmp": self.gmp,
            "gmp_percentage": self.gmp_percentage,
            "gmp_low": self.gmp_low,
            "gmp_high": self.gmp_high,
            "estimated_listing_price": self.estimated_listing_price,
            "subscription_times": self.subscription_times,
            "rating": self.rating,
            "pe_ratio": self.pe_ratio,
            "has_anchor_investors": self.has_anchor_investors,
            "source_updated_text": self.source_updated_text,
            "raw_data": self.raw_data,
        }


@dataclass(slots=True)
class ValidationIssue:
    """A single validation problem, tied to the record it came from."""

    record_index: int
    field_name: str
    message: str
    fatal: bool = False


@dataclass(slots=True)
class ValidationReport:
    """Outcome of validating a batch of normalized IPOs."""

    valid: list[NormalizedIPO] = field(default_factory=list)
    rejected: list[NormalizedIPO] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.valid) + len(self.rejected)

    @property
    def validity_ratio(self) -> float:
        return len(self.valid) / self.total if self.total else 0.0


@dataclass(slots=True)
class PersistenceResult:
    """Row counts from an upsert pass."""

    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    snapshots: int = 0


@dataclass(slots=True)
class ScrapeOutcome:
    """The full result of one pipeline execution, mirrored into ``scrape_runs``."""

    status: str
    strategy: ExtractionStrategy = ExtractionStrategy.NONE
    confidence: float = 0.0
    records_found: int = 0
    records_valid: int = 0
    records_invalid: int = 0
    persistence: PersistenceResult = field(default_factory=PersistenceResult)
    warnings: list[str] = field(default_factory=list)
    field_mapping: dict[str, str] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    http_status: int | None = None
    duration_ms: int | None = None
