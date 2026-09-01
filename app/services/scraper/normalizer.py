"""Turn extracted records into typed, canonical :class:`NormalizedIPO` values.

The normalizer owns every source-specific quirk, which keeps that knowledge out
of both the extractor (which only finds columns) and the repository (which only
writes rows).  Examples it handles:

* a single GMP cell carrying four values -
  ``"₹44 (24.86%) 44 ↓ / 55 ↑"`` -> premium, percentage, day low, day high;
* the exchange being encoded as a badge inside the name cell
  (``"BSE SME"``, ``"NSE SME"``, ``"IPO"``);
* a rating rendered as repeated emoji rather than a number;
* issue sizes written in crore/lakh with a currency symbol.

Anything unrecognised is preserved in ``raw_data`` rather than discarded.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from typing import Any

from app.core.logging import get_logger
from app.db.enums import Exchange, IPOType
from app.services.scraper.models import ExtractedRecord, NormalizedIPO
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

logger = get_logger(__name__)

_RATING_GLYPHS = ("\U0001f525", "⭐", "★")  # fire, star, black star
_POSITIVE_GLYPHS = ("✅", "✔")  # check marks
_NEGATIVE_GLYPHS = ("❌", "✖", "✘")  # crosses
_ANCHOR_RE = re.compile(r"<a\b[^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
# "5 ↓ / 12.50 ↑" - the day's observed GMP low and high.
_GMP_RANGE_RE = re.compile(
    r"([-+]?\d[\d,]*\.?\d*)\s*↓\s*/\s*([-+]?\d[\d,]*\.?\d*)\s*↑"
)

_TYPE_KEYWORDS: tuple[tuple[str, IPOType], ...] = (
    ("sme", IPOType.SME),
    ("mainboard", IPOType.MAINBOARD),
    ("main board", IPOType.MAINBOARD),
    ("ipo", IPOType.MAINBOARD),
)

_EXCHANGE_KEYWORDS: tuple[tuple[str, Exchange], ...] = (
    ("nse sme", Exchange.NSE_SME),
    ("nse emerge", Exchange.NSE_SME),
    ("emerge", Exchange.NSE_SME),
    ("bse sme", Exchange.BSE_SME),
    ("bse startup", Exchange.BSE_SME),
    ("nse bse", Exchange.NSE_BSE),
    ("bse nse", Exchange.NSE_BSE),
    ("nse", Exchange.NSE),
    ("bse", Exchange.BSE),
)

# Single-letter upstream status codes, kept only for provenance: the API's
# public status is always derived from the IPO's dates.
_KNOWN_STATUS_CODES = frozenset({"U", "O", "C", "L", "LP", "LN", "A"})


class IPONormalizer:
    """Convert :class:`ExtractedRecord` instances into canonical IPO values."""

    def __init__(self, reference_date: dt.date | None = None, base_url: str = "") -> None:
        # Anchors the year when the source prints dates without one ("3-Sep").
        self.reference_date = reference_date or dt.date.today()
        self.base_url = base_url.rstrip("/")

    def normalize_many(self, records: list[ExtractedRecord]) -> list[NormalizedIPO]:
        """Normalize a batch, skipping (and logging) individually broken rows."""
        results: list[NormalizedIPO] = []
        for index, record in enumerate(records):
            try:
                normalized = self.normalize(record)
            except Exception:
                logger.warning(
                    "scraper.record_normalization_failed",
                    extra={"record_index": index},
                    exc_info=True,
                )
                continue
            if normalized is not None:
                results.append(normalized)
        return results

    def normalize(self, record: ExtractedRecord) -> NormalizedIPO | None:
        """Normalize one record, or return ``None`` when it has no identity."""
        name = self._extract_name(record)
        if not name:
            return None

        detail_url = self._extract_detail_url(record)
        source_id = self._extract_source_id(record, detail_url, name)
        if not source_id:
            return None

        gmp, gmp_pct, gmp_low, gmp_high = self._extract_gmp(record)
        price_min, price_max = parse_price_band(record.get("price"))
        ipo_type = self._extract_ipo_type(record)

        normalized = NormalizedIPO(
            source_ipo_id=source_id,
            name=name,
            ipo_type=ipo_type.value,
            exchange=self._extract_exchange(record, ipo_type).value,
            source_status=self._extract_status(record),
            slug=self._extract_slug(detail_url),
            detail_url=detail_url,
            open_date=self._date(record.get("open_date")),
            close_date=self._date(record.get("close_date")),
            allotment_date=self._date(record.get("allotment_date")),
            listing_date=self._date(record.get("listing_date")),
            price_min=price_min,
            price_max=price_max,
            lot_size=self._positive_int(record.get("lot_size")),
            issue_size_crore=parse_amount_in_crore(record.get("issue_size")),
            gmp=gmp,
            gmp_percentage=gmp_pct,
            gmp_low=gmp_low,
            gmp_high=gmp_high,
            subscription_times=parse_multiplier(record.get("subscription")),
            rating=self._extract_rating(record.get("rating")),
            pe_ratio=parse_decimal(record.get("pe_ratio")),
            has_anchor_investors=self._extract_anchor(record.get("anchor")),
            source_updated_text=self._clean_text(record.get("updated_on")),
            raw_data=self._build_raw_data(record),
        )
        normalized.estimated_listing_price = self._estimated_listing_price(normalized)
        return normalized

    # ------------------------------------------------------------------
    # Field extraction
    # ------------------------------------------------------------------
    def _extract_name(self, record: ExtractedRecord) -> str | None:
        """Prefer the clean machine name; fall back to the anchor's own text."""
        for candidate in (record.get("name"), record.unmapped.get("name")):
            if candidate is None or is_null_token(candidate):
                continue
            raw = str(candidate)
            anchor = _ANCHOR_RE.search(raw)
            # The display cell appends badges after the link, so read the link
            # text rather than the whole cell.
            text = strip_html(anchor.group(1)) if anchor else strip_html(raw)
            if text and len(text) >= 2:
                return text[:255]
        return None

    def _extract_detail_url(self, record: ExtractedRecord) -> str | None:
        candidates = [record.get("detail_url"), record.get("name"), record.unmapped.get("name")]
        for candidate in candidates:
            if candidate is None or is_null_token(candidate):
                continue
            raw = str(candidate)
            href = first_link_href(raw) or (raw.strip() if raw.strip().startswith("/") else None)
            if href:
                if href.startswith("http"):
                    return href
                return f"{self.base_url}{href}" if self.base_url else href
        return None

    def _extract_source_id(
        self, record: ExtractedRecord, detail_url: str | None, name: str
    ) -> str | None:
        """Resolve a stable identity for this IPO.

        Falls back through the explicit id, then the numeric id embedded in the
        detail URL, then a slug of the name - so identity survives even if the
        id column disappears, at the cost of a weaker (but still stable) key.
        """
        explicit = record.get("source_ipo_id")
        if explicit is not None and not is_null_token(explicit):
            text = strip_html(explicit)
            if text:
                return text[:64]

        if detail_url:
            trailing = re.search(r"/(\d+)/?$", detail_url)
            if trailing:
                return trailing.group(1)

        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        return f"name:{slug}"[:64] if slug else None

    def _extract_gmp(
        self, record: ExtractedRecord
    ) -> tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None]:
        """Pull premium, percentage and the day's low/high out of the GMP cell."""
        cell = record.get("gmp")
        text = strip_html(cell) if cell is not None else ""

        gmp = None
        if text and not is_null_token(text):
            # The premium is the first number, before the "(x%)" and the range.
            leading = re.split(r"[(↓↑]", text, maxsplit=1)[0]
            gmp = parse_decimal(leading)

        # A dedicated percentage column is authoritative when present.
        percentage = parse_percentage(record.get("gmp_percentage"))
        if percentage is None and text:
            percentage = parse_percentage(text)

        low = high = None
        if cell is not None:
            match = _GMP_RANGE_RE.search(strip_html(cell))
            if match:
                low = parse_decimal(match.group(1))
                high = parse_decimal(match.group(2))
        return gmp, percentage, low, high

    @staticmethod
    def _all_badges(record: ExtractedRecord) -> list[str]:
        """Collect badge labels from every cell that might carry them.

        The mapped ``name`` field usually resolves to the source's *clean*
        machine column, which carries no markup - the exchange and status
        badges live in the separate display cell.  Both are scanned, so the
        badges are found whichever column the field mapper picked.
        """
        badges: list[str] = []
        for candidate in (
            record.get("name"),
            record.unmapped.get("name"),
            record.get("exchange"),
        ):
            if candidate is not None:
                badges.extend(extract_badges(candidate))
        return badges

    def _extract_ipo_type(self, record: ExtractedRecord) -> IPOType:
        haystacks: list[object] = [
            record.get("ipo_type"),
            record.unmapped.get("ipo category"),
            *self._all_badges(record),
        ]
        for value in haystacks:
            if value is None:
                continue
            text = strip_html(value).lower()
            for keyword, ipo_type in _TYPE_KEYWORDS:
                if keyword in text:
                    return ipo_type
        return IPOType.UNKNOWN

    def _extract_exchange(self, record: ExtractedRecord, ipo_type: IPOType) -> Exchange:
        """Read the exchange from badges, falling back to the board convention."""
        candidates: list[object] = [
            *self._all_badges(record),
            record.get("exchange"),
            record.get("ipo_type"),
        ]
        for value in candidates:
            if value is None:
                continue
            text = strip_html(value).lower()
            for keyword, exchange in _EXCHANGE_KEYWORDS:
                if keyword in text:
                    return exchange
        # Mainboard issues on this source list on both exchanges; SME issues
        # always name their platform, so an unnamed SME stays UNKNOWN.
        return Exchange.NSE_BSE if ipo_type is IPOType.MAINBOARD else Exchange.UNKNOWN

    def _extract_status(self, record: ExtractedRecord) -> str | None:
        raw = record.get("source_status")
        if raw is None or is_null_token(raw):
            return None
        text = strip_html(raw).upper()[:20]
        if text and text not in _KNOWN_STATUS_CODES:
            logger.debug("scraper.unknown_status_code", extra={"code": text})
        return text or None

    @staticmethod
    def _extract_rating(value: Any) -> int | None:
        """Count repeated rating glyphs, or read a plain numeric rating."""
        if value is None or is_null_token(value):
            return None
        text = strip_html(value)
        for glyph in _RATING_GLYPHS:
            count = text.count(glyph)
            if count:
                return min(count, 5)
        number = parse_int(text)
        return min(number, 5) if number is not None and 0 < number <= 10 else None

    @staticmethod
    def _extract_anchor(value: Any) -> bool | None:
        if value is None or is_null_token(value):
            return None
        text = strip_html(value)
        if any(glyph in text for glyph in _POSITIVE_GLYPHS):
            return True
        if any(glyph in text for glyph in _NEGATIVE_GLYPHS):
            return False
        lowered = text.lower()
        if lowered in {"yes", "y", "true"}:
            return True
        if lowered in {"no", "n", "false"}:
            return False
        return None

    @staticmethod
    def _extract_slug(detail_url: str | None) -> str | None:
        if not detail_url:
            return None
        parts = [p for p in detail_url.split("/") if p and not p.isdigit()]
        return parts[-1][:255] if parts else None

    @staticmethod
    def _estimated_listing_price(ipo: NormalizedIPO) -> Decimal | None:
        """Cap price plus premium - the figure the source headlines."""
        if ipo.price_max is None or ipo.gmp is None:
            return None
        return ipo.price_max + ipo.gmp

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _date(self, value: Any) -> dt.date | None:
        return parse_date(value, self.reference_date) if value is not None else None

    @staticmethod
    def _positive_int(value: Any) -> int | None:
        number = parse_int(value)
        return number if number is not None and number > 0 else None

    @staticmethod
    def _clean_text(value: Any) -> str | None:
        if value is None or is_null_token(value):
            return None
        return strip_html(value)[:64] or None

    @staticmethod
    def _build_raw_data(record: ExtractedRecord) -> dict[str, Any]:
        """Preserve every unmapped source value as plain text.

        This is what makes a newly-added upstream column survive: it is stored
        from the first scrape and can be promoted to a real column later
        without back-filling from the source.
        """
        raw: dict[str, Any] = {}
        for key, value in record.unmapped.items():
            text = strip_html(value)
            if text and not is_null_token(text):
                raw[key] = text[:500]
        return raw
