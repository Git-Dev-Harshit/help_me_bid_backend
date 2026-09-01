"""Tolerant parsers for the messy values that appear in scraped markup.

Upstream cells arrive as things like ``"&#8377;<b>44</b> (24.86%)"``,
``"₹459.72 Cr"``, ``"0.88x"`` or ``"1-Sep"``.  Every helper here returns
``None`` instead of raising when a value cannot be understood: a single
unreadable cell must never abort the whole scrape, it should simply leave that
one field empty and be reflected in the confidence score.
"""

from __future__ import annotations

import datetime as dt
import html
import re
import unicodedata
from decimal import Decimal, InvalidOperation

# Multipliers for Indian financial magnitude suffixes. Values are normalised to
# crore, which is the unit InvestorGain reports issue sizes in.
_CRORE = Decimal(1)
_MAGNITUDES: dict[str, Decimal] = {
    "cr": _CRORE,
    "crore": _CRORE,
    "crores": _CRORE,
    "lakh": Decimal("0.01"),
    "lakhs": Decimal("0.01"),
    "lac": Decimal("0.01"),
    "lacs": Decimal("0.01"),
    "k": Decimal("0.0001"),
    "thousand": Decimal("0.0001"),
}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_NUMBER_RE = re.compile(r"[-+]?\d[\d,]*\.?\d*")
_PERCENT_RE = re.compile(r"\(?\s*([-+]?\d[\d,]*\.?\d*)\s*%\s*\)?")
_MULTIPLIER_RE = re.compile(r"([-+]?\d[\d,]*\.?\d*)\s*x", re.IGNORECASE)
_MAGNITUDE_RE = re.compile(
    r"([-+]?\d[\d,]*\.?\d*)\s*(cr(?:ores?)?|lakhs?|lacs?|thousand|k)\b",
    re.IGNORECASE,
)
# A range needs digits on BOTH sides of the separator, so a lone negative
# number ("-5") is never mistaken for one.
_RANGE_RE = re.compile(
    # The en/em dashes are intentional, not typos: pages really do use them as
    # range separators, so the pattern has to accept both.
    r"(\d[\d,]*\.?\d*)\s*(?:-{1,2}|to|–|—|/)\s*(\d[\d,]*\.?\d*)",  # noqa: RUF001
    re.IGNORECASE,
)

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

#: Month names understood by :func:`parse_date`; exported so the scraper's
#: field map can build date-shaped detection patterns from one source of truth.
MONTH_NAMES: frozenset[str] = frozenset(_MONTHS)

# Values that mean "no data" rather than a real zero.
_NULL_TOKENS = frozenset({"", "-", "--", "---", "n/a", "na", "nil", "none", "tba", "?"})


def strip_html(value: object) -> str:
    """Reduce an HTML fragment to collapsed, entity-decoded plain text."""
    if value is None:
        return ""
    text = str(value)
    # <br> carries meaning (it separates stacked values), so keep a space.
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = unicodedata.normalize("NFKC", text)
    return _WS_RE.sub(" ", text).strip()


def is_null_token(value: object) -> bool:
    """True when a cell is one of the many spellings of "empty"."""
    return strip_html(value).strip().lower() in _NULL_TOKENS


def parse_decimal(value: object) -> Decimal | None:
    """Extract the first decimal number from a value.

    Understands currency symbols, thousands separators and surrounding markup.
    """
    text = strip_html(value)
    if is_null_token(text):
        return None
    match = _NUMBER_RE.search(text)
    if not match:
        return None
    try:
        return Decimal(match.group(0).replace(",", ""))
    except InvalidOperation:
        return None


def parse_int(value: object) -> int | None:
    """Extract an integer, tolerating decimal input by truncating."""
    number = parse_decimal(value)
    if number is None:
        return None
    try:
        return int(number)
    except (ValueError, OverflowError):
        return None


def parse_percentage(value: object) -> Decimal | None:
    """Extract a percentage, with or without a literal ``%``.

    ``"(24.86%)"`` and ``"24.86"`` both yield ``Decimal("24.86")``.
    """
    text = strip_html(value)
    if is_null_token(text):
        return None
    match = _PERCENT_RE.search(text)
    if match:
        try:
            return Decimal(match.group(1).replace(",", ""))
        except InvalidOperation:
            return None
    return parse_decimal(text)


def parse_multiplier(value: object) -> Decimal | None:
    """Parse a subscription figure such as ``"111.18x"``."""
    text = strip_html(value)
    if is_null_token(text):
        return None
    match = _MULTIPLIER_RE.search(text)
    if match:
        try:
            return Decimal(match.group(1).replace(",", ""))
        except InvalidOperation:
            return None
    return parse_decimal(text)


def parse_amount_in_crore(value: object) -> Decimal | None:
    """Parse a money amount and normalise it to crore.

    ``"₹459.72 Cr"`` -> ``459.72``; ``"250 Lakh"`` -> ``2.50``.
    A bare number is assumed to already be in crore, which matches how the
    source reports issue sizes.
    """
    text = strip_html(value)
    if is_null_token(text):
        return None
    match = _MAGNITUDE_RE.search(text)
    if match:
        try:
            amount = Decimal(match.group(1).replace(",", ""))
        except InvalidOperation:
            return None
        return amount * _MAGNITUDES.get(match.group(2).lower(), _CRORE)
    return parse_decimal(text)


def parse_price_band(value: object) -> tuple[Decimal | None, Decimal | None]:
    """Parse a price band into ``(min, max)``.

    Handles ``"100-110"``, ``"100 to 110"`` and a single ``"177"`` (in which
    case both ends are the same value).
    """
    text = strip_html(value)
    if is_null_token(text):
        return None, None

    # Match the range as a whole first. Scanning for numbers individually would
    # read the hyphen in "100-110" as a minus sign and yield (-110, 100).
    band = _RANGE_RE.search(text)
    if band:
        try:
            low, high = Decimal(band.group(1).replace(",", "")), Decimal(
                band.group(2).replace(",", "")
            )
        except InvalidOperation:
            return None, None
        return min(low, high), max(low, high)

    single = parse_decimal(text)
    return (single, single) if single is not None else (None, None)


def parse_date(value: object, reference: dt.date | None = None) -> dt.date | None:
    """Parse the date formats the source is known to emit.

    Recognises ISO (``2026-09-03``), ``1-Sep``, ``01-Sep-2026``, ``1 Sep 2026``
    and ``03/09/2026``.  When the year is absent it is inferred as the one that
    places the date closest to ``reference`` (today by default) - that is what
    keeps a ``"1-Jan"`` close date parsed in late December from landing eleven
    months in the past.
    """
    text = strip_html(value)
    if is_null_token(text):
        return None
    reference = reference or dt.date.today()

    # ISO first: unambiguous and the format the source's sort columns use.
    iso = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if iso:
        return _safe_date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))

    # Day + month name, with an optional year.
    named = re.search(
        r"\b(\d{1,2})\s*[-/ ]\s*([A-Za-z]{3,9})\.?(?:\s*[-/, ]\s*(\d{2,4}))?",
        text,
    )
    if named:
        month = _MONTHS.get(named.group(2).lower())
        if month:
            day = int(named.group(1))
            if named.group(3):
                return _safe_date(_expand_year(int(named.group(3))), month, day)
            return _infer_year(month, day, reference)

    # Month name first: "Sep 3, 2026".
    named_first = re.search(
        r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:\s*[-/, ]\s*(\d{2,4}))?",
        text,
    )
    if named_first:
        month = _MONTHS.get(named_first.group(1).lower())
        if month:
            day = int(named_first.group(2))
            if named_first.group(3):
                return _safe_date(_expand_year(int(named_first.group(3))), month, day)
            return _infer_year(month, day, reference)

    # Numeric day/month/year. Indian sources are day-first.
    numeric = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b", text)
    if numeric:
        return _safe_date(
            _expand_year(int(numeric.group(3))), int(numeric.group(2)), int(numeric.group(1))
        )
    return None


def _safe_date(year: int, month: int, day: int) -> dt.date | None:
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def _expand_year(year: int) -> int:
    """Expand a two-digit year (``26`` -> ``2026``)."""
    if year < 100:
        return 2000 + year
    return year


def _infer_year(month: int, day: int, reference: dt.date) -> dt.date | None:
    """Choose the year that puts ``month``/``day`` nearest to ``reference``."""
    candidates = [
        candidate
        for year in (reference.year - 1, reference.year, reference.year + 1)
        if (candidate := _safe_date(year, month, day)) is not None
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda d: abs((d - reference).days))


def count_occurrences(value: object, needle: str) -> int:
    """Count repeats of a character - used for the emoji star/fire rating."""
    return strip_html(value).count(needle)


def first_link_href(value: object) -> str | None:
    """Return the first ``href`` in an HTML fragment."""
    if value is None:
        return None
    match = re.search(r'href=["\']([^"\']+)["\']', str(value))
    return match.group(1) if match else None


def extract_badges(value: object) -> list[str]:
    """Return the text of every ``badge``-classed span in a fragment.

    The source encodes both the exchange/board (``"NSE SME"``) and a status
    letter (``"O"``) as badges next to the IPO name.
    """
    if value is None:
        return []
    fragments = re.findall(
        r'<span[^>]*class=["\'][^"\']*badge[^"\']*["\'][^>]*>(.*?)</span>',
        str(value),
        flags=re.IGNORECASE | re.DOTALL,
    )
    return [text for fragment in fragments if (text := strip_html(fragment))]
