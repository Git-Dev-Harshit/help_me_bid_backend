"""Extraction strategies: turn a raw payload into canonical-field records.

Two strategies are registered, tried in order by :class:`ExtractorChain`:

``JsonApiStrategy``
    Reads the JSON feed the page's own client-side code calls.  This is the
    primary path: the upstream page is a client-rendered Next.js app whose
    server HTML contains an *empty* table, so there is nothing for an HTML
    parser to read.  The feed also exposes pre-parsed machine columns (stable
    id, ISO dates, plain decimals), which makes it both lighter and more
    reliable than rendering a browser.

``HtmlTableStrategy``
    A structure-driven HTML parser used when the JSON feed is unavailable or
    unrecognisable - and the path that keeps working if the source ever returns
    to server-side rendering.

Neither strategy hard-codes a CSS selector or a column position: both discover
the dataset by shape and identify columns through
:mod:`app.services.scraper.field_map`.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any

from bs4 import BeautifulSoup, Tag

from app.core.logging import get_logger
from app.db.enums import ExtractionStrategy
from app.services.scraper.field_map import MappingResult, map_columns, normalize_label
from app.services.scraper.models import ExtractedRecord, ExtractionResult, RawPayload
from app.utils.parsing import is_null_token, strip_html

logger = get_logger(__name__)

#: A dataset must contribute at least this much confidence to be usable.
MIN_STRATEGY_CONFIDENCE = 0.35
#: Tables with fewer rows than this are almost certainly not the IPO dataset.
MIN_PLAUSIBLE_ROWS = 1
#: A dataset needs at least this many columns to carry distinct IPO fields.
MIN_DATASET_COLUMNS = 2


def _score_mapping(mapping: MappingResult, row_count: int) -> float:
    """Confidence that a mapped dataset really is the IPO table.

    Blends how many *required* fields were located with whether the rows can be
    identified at all, then tapers the score for implausibly small datasets.
    """
    if row_count < MIN_PLAUSIBLE_ROWS:
        return 0.0
    coverage = mapping.required_coverage
    identity = 1.0 if mapping.has_identity else 0.0
    # Identity is weighted heavily: without it, rows cannot be de-duplicated
    # against existing records and must not be persisted.
    score = (coverage * 0.7) + (identity * 0.3)
    if row_count < 3:
        score *= 0.8
    return round(min(score, 1.0), 3)


class ExtractionStrategyBase(ABC):
    """Common interface for every extraction strategy."""

    strategy: ExtractionStrategy

    @abstractmethod
    def can_handle(self, payload: RawPayload) -> bool:
        """Cheap check for whether this strategy is worth attempting."""

    @abstractmethod
    def extract(self, payload: RawPayload) -> ExtractionResult:
        """Produce records, a field mapping and a confidence score."""

    def _build_result(
        self,
        rows: list[dict[str, Any]],
        columns: list[str],
    ) -> ExtractionResult:
        """Map columns then project every row onto canonical field names."""
        mapping = map_columns(columns, rows)
        records: list[ExtractedRecord] = []
        inverted = {column: field for field, column in mapping.mapping.items()}

        for row in rows:
            record = ExtractedRecord()
            for column, value in row.items():
                canonical = inverted.get(column)
                if canonical:
                    record.fields[canonical] = value
                elif not is_null_token(value):
                    # Retained verbatim so a newly added upstream column is
                    # never silently dropped; lands in the ipos.raw_data JSONB.
                    record.unmapped[normalize_label(column) or column] = value
            records.append(record)

        return ExtractionResult(
            strategy=self.strategy,
            records=records,
            field_mapping=dict(mapping.mapping),
            unmapped_columns=mapping.unmapped_columns,
            warnings=list(mapping.warnings),
            confidence=_score_mapping(mapping, len(records)),
        )


class JsonApiStrategy(ExtractionStrategyBase):
    """Extract IPO rows from the upstream JSON feed.

    The row list is *located by shape* rather than by key name: any list of
    similarly-keyed dicts anywhere in the document qualifies.  If the feed is
    ever renamed from ``reportTableData`` to something else, extraction still
    succeeds.
    """

    strategy = ExtractionStrategy.JSON_API

    def can_handle(self, payload: RawPayload) -> bool:
        if payload.is_json:
            return True
        stripped = payload.content.lstrip()
        return stripped.startswith(("{", "["))

    def extract(self, payload: RawPayload) -> ExtractionResult:
        try:
            document = json.loads(payload.content)
        except (json.JSONDecodeError, ValueError) as exc:
            return ExtractionResult(
                strategy=self.strategy,
                warnings=[f"payload is not valid JSON: {exc}"],
            )

        rows = self._locate_row_list(document)
        if not rows:
            return ExtractionResult(
                strategy=self.strategy,
                warnings=["no list of row objects found in the JSON payload"],
            )

        flattened = [self._flatten_row(row) for row in rows]
        columns = self._ordered_columns(flattened)
        return self._build_result(flattened, columns)

    # -- helpers -------------------------------------------------------
    def _locate_row_list(self, node: Any, depth: int = 0) -> list[dict[str, Any]]:
        """Find the largest list of consistently-keyed dicts in the document."""
        if depth > 6:
            return []
        best: list[dict[str, Any]] = []

        if isinstance(node, list) and self._is_row_list(node):
            best = node
        elif isinstance(node, dict):
            for value in node.values():
                candidate = self._locate_row_list(value, depth + 1)
                if len(candidate) > len(best):
                    best = candidate
        elif isinstance(node, list):
            for value in node:
                candidate = self._locate_row_list(value, depth + 1)
                if len(candidate) > len(best):
                    best = candidate
        return best

    @staticmethod
    def _is_row_list(node: list[Any]) -> bool:
        """True when a list looks like tabular rows (dicts sharing most keys)."""
        dicts = [item for item in node if isinstance(item, dict)]
        if not dicts or len(dicts) < len(node) * 0.8:
            return False
        if len(dicts[0]) < 3:
            return False
        first_keys = set(dicts[0])
        shared = sum(
            1 for item in dicts[:10] if len(first_keys & set(item)) >= len(first_keys) * 0.6
        )
        return shared >= min(len(dicts), 10) * 0.8

    @staticmethod
    def _flatten_row(row: dict[str, Any]) -> dict[str, Any]:
        """Unwrap per-cell envelopes.

        The feed wraps display cells as ``{"value": "<html>"}``; machine columns
        are bare scalars.  Both are reduced to the underlying value.
        """
        flat: dict[str, Any] = {}
        for key, value in row.items():
            if isinstance(value, dict):
                for candidate in ("value", "val", "text", "display", "html"):
                    if candidate in value:
                        flat[key] = value[candidate]
                        break
                else:
                    flat[key] = json.dumps(value, ensure_ascii=False)
            else:
                flat[key] = value
        return flat

    @staticmethod
    def _ordered_columns(rows: list[dict[str, Any]]) -> list[str]:
        """Union of keys across rows, preserving first-seen order."""
        columns: dict[str, None] = {}
        for row in rows:
            for key in row:
                columns.setdefault(key, None)
        return list(columns)


class HtmlTableStrategy(ExtractionStrategyBase):
    """Locate and read an IPO dataset inside an HTML document.

    Candidate datasets are gathered from three structural patterns, scored with
    the same field-mapping confidence, and the best one wins:

    1. real ``<table>`` elements (with or without ``<thead>``);
    2. ARIA grids (``role="table"``/``"row"``/``"cell"``) built from ``<div>``s;
    3. repeated label/value blocks ("card" layouts).

    Class names and ids are never used for selection, so a restyled page is
    still read correctly.
    """

    strategy = ExtractionStrategy.HTML_TABLE

    def can_handle(self, payload: RawPayload) -> bool:
        return "<" in payload.content and not payload.is_json

    def extract(self, payload: RawPayload) -> ExtractionResult:
        # lxml is fast and tolerant of the malformed markup real pages contain.
        soup = BeautifulSoup(payload.content, "lxml")

        best: ExtractionResult | None = None
        warnings: list[str] = []
        for rows, columns, origin in self._candidate_datasets(soup):
            if not rows:
                continue
            result = self._build_result(rows, columns)
            result.warnings.insert(0, f"dataset located via {origin}")
            if best is None or result.confidence > best.confidence:
                best = result

        if best is None:
            warnings.append("no tabular IPO dataset could be located in the HTML")
            return ExtractionResult(strategy=self.strategy, warnings=warnings)
        return best

    # -- candidate discovery -------------------------------------------
    def _candidate_datasets(
        self, soup: BeautifulSoup
    ) -> list[tuple[list[dict[str, Any]], list[str], str]]:
        candidates: list[tuple[list[dict[str, Any]], list[str], str]] = []
        for table in soup.find_all("table"):
            rows, columns = self._read_table(table)
            if rows:
                candidates.append((rows, columns, "table element"))
        for grid in soup.find_all(attrs={"role": "table"}) + soup.find_all(attrs={"role": "grid"}):
            rows, columns = self._read_aria_grid(grid)
            if rows:
                candidates.append((rows, columns, "ARIA grid"))
        rows, columns = self._read_label_value_blocks(soup)
        if rows:
            candidates.append((rows, columns, "repeated label/value blocks"))
        return candidates

    def _read_table(self, table: Tag) -> tuple[list[dict[str, Any]], list[str]]:
        """Read a ``<table>``, tolerating a missing ``<thead>``."""
        all_rows = table.find_all("tr")
        if not all_rows:
            return [], []

        header_cells: list[str] = []
        body_rows: list[Tag] = []

        # Prefer an explicit header row; otherwise treat a leading all-<th> or
        # non-numeric first row as the header.
        for index, row in enumerate(all_rows):
            cells = row.find_all(["th", "td"])
            if not cells:
                continue
            if not header_cells and (
                all(cell.name == "th" for cell in cells)
                or (index == 0 and self._looks_like_header(cells))
            ):
                header_cells = [strip_html(cell.decode_contents()) for cell in cells]
                continue
            body_rows.append(row)

        if not body_rows:
            return [], []

        width = max(len(row.find_all(["th", "td"])) for row in body_rows)
        # A single-column table cannot be a multi-field IPO dataset; it is a
        # notice, a layout table or a "No data available" placeholder.
        if width < MIN_DATASET_COLUMNS:
            return [], []
        columns = self._pad_headers(header_cells, width)

        rows: list[dict[str, Any]] = []
        for row in body_rows:
            cells = row.find_all(["th", "td"])
            if not cells:
                continue
            # A single-cell row is a "no data" placeholder or a spanning note.
            if len(cells) == 1 and width > 1:
                continue
            rows.append(
                {
                    columns[i]: cell.decode_contents()
                    for i, cell in enumerate(cells)
                    if i < len(columns)
                }
            )
        return rows, columns

    def _read_aria_grid(self, grid: Tag) -> tuple[list[dict[str, Any]], list[str]]:
        """Read a div-based grid that declares ARIA table semantics."""
        row_nodes = grid.find_all(attrs={"role": "row"})
        if not row_nodes:
            return [], []

        header_cells: list[str] = []
        body: list[list[Tag]] = []
        for row in row_nodes:
            headers = row.find_all(attrs={"role": "columnheader"})
            if headers and not header_cells:
                header_cells = [strip_html(cell.decode_contents()) for cell in headers]
                continue
            cells = row.find_all(attrs={"role": "cell"}) or row.find_all(
                attrs={"role": "gridcell"}
            )
            if cells:
                body.append(cells)

        if not body:
            return [], []
        width = max(len(cells) for cells in body)
        if width < MIN_DATASET_COLUMNS:
            return [], []
        columns = self._pad_headers(header_cells, width)
        rows = [
            {columns[i]: cell.decode_contents() for i, cell in enumerate(cells) if i < len(columns)}
            for cells in body
        ]
        return rows, columns

    def _read_label_value_blocks(
        self, soup: BeautifulSoup
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Read repeated "card" blocks of ``<dt>``/``<dd>`` or label/value pairs.

        A last-resort structure for pages that abandon tables entirely.
        """
        rows: list[dict[str, Any]] = []
        for block in soup.find_all(["dl", "article", "li"]):
            pairs: dict[str, Any] = {}
            terms = block.find_all("dt")
            for term in terms:
                definition = term.find_next_sibling("dd")
                if definition is not None:
                    label = strip_html(term.decode_contents())
                    if label:
                        pairs[label] = definition.decode_contents()
            if len(pairs) >= 4:
                rows.append(pairs)

        if len(rows) < 2:
            return [], []
        columns: dict[str, None] = {}
        for row in rows:
            for key in row:
                columns.setdefault(key, None)
        return rows, list(columns)

    # -- small helpers -------------------------------------------------
    @staticmethod
    def _looks_like_header(cells: list[Tag]) -> bool:
        """A header row is short, textual and free of numbers."""
        texts = [strip_html(cell.decode_contents()) for cell in cells]
        filled = [text for text in texts if text]
        if len(filled) < 2:
            return False
        wordy = sum(
            1 for text in filled if any(c.isalpha() for c in text) and len(text) <= 40
        )
        return wordy >= len(filled) * 0.7

    @staticmethod
    def _pad_headers(headers: list[str], width: int) -> list[str]:
        """Ensure one usable column label per cell position."""
        columns = [h if h else f"column_{i}" for i, h in enumerate(headers)]
        while len(columns) < width:
            columns.append(f"column_{len(columns)}")
        return columns


class ExtractorChain:
    """Try each strategy in order and return the most confident result.

    Falling through to the next strategy (rather than failing) is what lets the
    scraper survive an upstream change of delivery mechanism.
    """

    def __init__(self, strategies: list[ExtractionStrategyBase] | None = None) -> None:
        self.strategies = strategies or [JsonApiStrategy(), HtmlTableStrategy()]

    def extract(self, payload: RawPayload) -> ExtractionResult:
        attempts: list[ExtractionResult] = []
        for strategy in self.strategies:
            if not strategy.can_handle(payload):
                continue
            result = strategy.extract(payload)
            attempts.append(result)
            logger.debug(
                "scraper.strategy_attempted",
                extra={
                    "strategy": result.strategy.value,
                    "records": len(result.records),
                    "confidence": result.confidence,
                },
            )
            if result.succeeded and result.confidence >= MIN_STRATEGY_CONFIDENCE:
                return result

        if not attempts:
            return ExtractionResult(
                strategy=ExtractionStrategy.NONE,
                warnings=["no extraction strategy could handle the payload"],
            )
        # Nothing cleared the bar - return the closest attempt so the failure is
        # reported with real diagnostics rather than an empty result.
        return max(attempts, key=lambda r: (r.confidence, len(r.records)))
