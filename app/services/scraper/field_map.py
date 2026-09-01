"""Semantic field mapping - the core of the scraper's resilience.

The scraper never says "the IPO name is the second ``<td>``" or "GMP lives in
``table.ipo-table td.gmp``".  Instead each canonical field declares *what it
looks like*: the header labels that mean it, and the shape its values take.
Columns are then identified by meaning, so renamed classes, reordered columns,
extra wrapper elements and relabelled headers all survive.

Identification runs in descending order of trust:

1. **Exact label match** - the normalised header equals a known alias.
2. **Token-subset match** - every token of an alias appears in the header
   ("Close" matches "Close Date", "IPO Close Dt").
3. **Substring match** - a weaker containment check.
4. **Value-shape inference** - the header is unrecognised, so the column's
   *values* are sampled and matched against the field's expected shape (all
   ISO dates, all ``12.5x`` multiples, ...).

Every match carries a score; the highest-scoring column wins a field, and a
field's overall coverage feeds the run's confidence score.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.utils.parsing import (
    MONTH_NAMES,
    is_null_token,
    parse_date,
    parse_decimal,
    strip_html,
)

# --- Scores -----------------------------------------------------------------
SCORE_EXACT = 1.0
SCORE_TOKEN_SUBSET = 0.8
SCORE_SUBSTRING = 0.6
SCORE_VALUE_SHAPE = 0.45
# The upstream JSON exposes pre-parsed machine columns prefixed with "~"
# (ISO dates, plain decimals). They beat the display columns when both exist.
MACHINE_COLUMN_BONUS = 0.25

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_ISO_DATE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$")
_MULTIPLIER = re.compile(r"^[-+]?\d[\d,]*\.?\d*\s*x$", re.IGNORECASE)
_PERCENT = re.compile(r"\d\s*%")
_MONTH_NAMES = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
_DAY_MONTH = re.compile(rf"^\d{{1,2}}\s*[-/ ]\s*({_MONTH_NAMES})\.?", re.IGNORECASE)


def normalize_label(label: str) -> str:
    """Reduce a header to comparable tokens.

    ``"~Srt_Close"``, ``"Close Date"`` and ``"CLOSE-DATE"`` all normalise to a
    space-separated lowercase token string.
    """
    text = strip_html(label).lower().lstrip("~").strip()
    text = _NON_ALNUM.sub(" ", text)
    return " ".join(text.split())


def _tokens(label: str) -> set[str]:
    return set(normalize_label(label).split())


# --- Value shape predicates -------------------------------------------------
def _looks_like_date(value: Any) -> bool:
    text = strip_html(value)
    if is_null_token(text):
        return False
    return bool(_ISO_DATE.match(text) or _DAY_MONTH.match(text)) or parse_date(text) is not None


def _looks_like_number(value: Any) -> bool:
    text = strip_html(value)
    if is_null_token(text):
        return False
    return parse_decimal(text) is not None


def _looks_like_multiplier(value: Any) -> bool:
    text = strip_html(value)
    return bool(_MULTIPLIER.match(text)) if not is_null_token(text) else False


def _looks_like_percent(value: Any) -> bool:
    text = strip_html(value)
    return bool(_PERCENT.search(text)) if not is_null_token(text) else False


def _looks_like_currency(value: Any) -> bool:
    text = strip_html(str(value))
    if is_null_token(text):
        return False
    return ("₹" in text or "Rs" in text or "cr" in text.lower()) and _looks_like_number(text)


def _looks_like_name(value: Any) -> bool:
    text = strip_html(value)
    # A name is mostly letters and cannot be parsed as a bare number.
    return len(text) >= 3 and any(c.isalpha() for c in text) and parse_decimal(text) is None


@dataclass(frozen=True, slots=True)
class CanonicalField:
    """Declarative description of one field the scraper wants to find."""

    name: str
    aliases: tuple[str, ...]
    #: Predicate used for value-shape inference when the header is unknown.
    shape: Callable[[Any], bool] | None = None
    #: Required fields dominate the confidence score; without them the dataset
    #: is not recognisably an IPO table.
    required: bool = False
    #: Identity fields must be present for a record to be persistable at all.
    identity: bool = False
    #: Fraction of sampled values that must satisfy ``shape`` to infer a match.
    shape_threshold: float = 0.6

    def score_label(self, label: str) -> float:
        """Score how well a source column header matches this field."""
        normalized = normalize_label(label)
        if not normalized:
            return 0.0
        bonus = MACHINE_COLUMN_BONUS if str(label).startswith("~") else 0.0

        alias_norms = [normalize_label(a) for a in self.aliases]
        if normalized in alias_norms:
            return SCORE_EXACT + bonus

        header_tokens = _tokens(normalized)
        for alias in alias_norms:
            alias_tokens = _tokens(alias)
            if alias_tokens and alias_tokens.issubset(header_tokens):
                return SCORE_TOKEN_SUBSET + bonus
        for alias in alias_norms:
            if alias and (alias in normalized or normalized in alias):
                return SCORE_SUBSTRING + bonus
        return 0.0

    def score_values(self, values: Sequence[Any]) -> float:
        """Score this field against a column's values (header ignored)."""
        if self.shape is None:
            return 0.0
        samples = [v for v in values if not is_null_token(v)]
        if len(samples) < 2:
            return 0.0
        hits = sum(1 for v in samples if self.shape(v))
        ratio = hits / len(samples)
        return SCORE_VALUE_SHAPE * ratio if ratio >= self.shape_threshold else 0.0


# ---------------------------------------------------------------------------
# The canonical schema
# ---------------------------------------------------------------------------
# Aliases cover the current upstream column names, the machine ("~") columns,
# and the labels a human-facing HTML table would plausibly use.  Adding support
# for a new upstream field means adding one entry here - nothing else changes.
CANONICAL_FIELDS: tuple[CanonicalField, ...] = (
    CanonicalField(
        name="source_ipo_id",
        aliases=("id", "ipo id", "issue id", "company id"),
        required=True,
        identity=True,
    ),
    CanonicalField(
        name="name",
        aliases=("ipo name", "name", "company", "company name", "issue", "issuer", "ipo"),
        shape=_looks_like_name,
        required=True,
        identity=True,
    ),
    CanonicalField(
        name="ipo_type",
        aliases=("ipo category", "category", "type", "ipo type", "board", "segment"),
    ),
    CanonicalField(
        name="source_status",
        aliases=("ipo status", "status", "state"),
    ),
    CanonicalField(
        name="gmp",
        aliases=("gmp", "grey market premium", "gray market premium", "premium", "max gmp"),
        shape=_looks_like_currency,
        required=True,
    ),
    CanonicalField(
        name="gmp_percentage",
        aliases=(
            "gmp percent calc", "gmp percent", "gmp percentage", "gmp %",
            "estimated listing gain", "listing gain", "est listing gain", "gain",
        ),
        shape=_looks_like_percent,
    ),
    CanonicalField(
        name="price",
        aliases=("price", "issue price", "price band", "cap price", "offer price"),
        shape=_looks_like_number,
        required=True,
    ),
    CanonicalField(
        name="lot_size",
        aliases=("lot", "lot size", "market lot", "min qty", "minimum quantity"),
        shape=_looks_like_number,
    ),
    CanonicalField(
        name="issue_size",
        aliases=("ipo size", "issue size", "size", "offer size", "total issue size"),
        shape=_looks_like_currency,
    ),
    CanonicalField(
        name="subscription",
        aliases=("sub", "subscription", "subscribed", "times subscribed", "sub times"),
        shape=_looks_like_multiplier,
    ),
    CanonicalField(
        name="open_date",
        aliases=("srt open", "open", "open date", "opening date", "start date", "issue open"),
        shape=_looks_like_date,
        required=True,
    ),
    CanonicalField(
        name="close_date",
        aliases=(
            "srt close", "close", "close date", "closing date", "last date",
            "issue close", "end date",
        ),
        shape=_looks_like_date,
        required=True,
    ),
    CanonicalField(
        name="allotment_date",
        aliases=(
            "srt boa dt", "boa dt", "boa", "allotment", "allotment date",
            "basis of allotment", "allotment dt",
        ),
        shape=_looks_like_date,
    ),
    CanonicalField(
        name="listing_date",
        aliases=("str listing", "srt listing", "listing", "listing date", "list date"),
        shape=_looks_like_date,
    ),
    CanonicalField(
        name="rating",
        aliases=("rating", "review", "score", "stars"),
    ),
    CanonicalField(
        name="pe_ratio",
        aliases=("p e", "pe", "pe ratio", "p e ratio", "price earnings"),
    ),
    CanonicalField(
        name="anchor",
        aliases=("anchor", "anchor investor", "anchor investors"),
    ),
    CanonicalField(
        name="updated_on",
        aliases=("updated on", "updated", "last updated", "update time"),
    ),
    CanonicalField(
        name="detail_url",
        aliases=("urlrewrite folder name", "url", "link", "detail url", "slug"),
    ),
)

FIELDS_BY_NAME: dict[str, CanonicalField] = {f.name: f for f in CANONICAL_FIELDS}
REQUIRED_FIELDS: tuple[str, ...] = tuple(f.name for f in CANONICAL_FIELDS if f.required)
IDENTITY_FIELDS: tuple[str, ...] = tuple(f.name for f in CANONICAL_FIELDS if f.identity)


@dataclass(slots=True)
class ColumnMatch:
    """A resolved column -> canonical field association."""

    field_name: str
    column: str
    score: float
    method: str


@dataclass(slots=True)
class MappingResult:
    """The outcome of mapping a source's columns onto canonical fields."""

    mapping: dict[str, str] = field(default_factory=dict)  # field -> column
    matches: list[ColumnMatch] = field(default_factory=list)
    unmapped_columns: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def required_coverage(self) -> float:
        """Fraction of required canonical fields that were located."""
        if not REQUIRED_FIELDS:
            return 1.0
        found = sum(1 for name in REQUIRED_FIELDS if name in self.mapping)
        return found / len(REQUIRED_FIELDS)

    @property
    def has_identity(self) -> bool:
        """True when at least one identity field was located.

        Without an identity the rows cannot be de-duplicated against existing
        records, so the run must not be allowed to persist.
        """
        return any(name in self.mapping for name in IDENTITY_FIELDS)

    def missing_required(self) -> list[str]:
        return [name for name in REQUIRED_FIELDS if name not in self.mapping]


def map_columns(
    columns: Sequence[str],
    sample_rows: Sequence[dict[str, Any]] | None = None,
) -> MappingResult:
    """Associate source columns with canonical fields.

    ``sample_rows`` enables value-shape inference for columns whose header is
    unrecognisable; pass a handful of rows (all of them is fine) to let a
    renamed column still be identified by the data it carries.
    """
    result = MappingResult()
    sample_rows = list(sample_rows or [])

    # Score every (field, column) pair by label, then by value shape.
    candidates: list[ColumnMatch] = []
    for column in columns:
        column_values = [row.get(column) for row in sample_rows[:25]]
        for canonical in CANONICAL_FIELDS:
            label_score = canonical.score_label(column)
            if label_score > 0:
                candidates.append(
                    ColumnMatch(canonical.name, column, label_score, "label")
                )
                continue
            if column_values:
                shape_score = canonical.score_values(column_values)
                if shape_score > 0:
                    candidates.append(
                        ColumnMatch(canonical.name, column, shape_score, "value_shape")
                    )

    # Greedy assignment, best score first. A field and a column are each used
    # once, so two similarly-named columns cannot collapse onto one field.
    candidates.sort(key=lambda m: m.score, reverse=True)
    used_columns: set[str] = set()
    for candidate in candidates:
        if candidate.field_name in result.mapping or candidate.column in used_columns:
            continue
        result.mapping[candidate.field_name] = candidate.column
        result.matches.append(candidate)
        used_columns.add(candidate.column)

    result.unmapped_columns = [c for c in columns if c not in used_columns]

    for name in result.missing_required():
        result.warnings.append(f"required field not found in source: {name}")
    for match in result.matches:
        if match.method == "value_shape":
            result.warnings.append(
                f"field {match.field_name!r} matched column {match.column!r} by value shape "
                "- the upstream header may have been renamed"
            )
    return result
