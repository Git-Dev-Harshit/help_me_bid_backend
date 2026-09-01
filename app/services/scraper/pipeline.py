"""The scrape pipeline: fetch -> extract -> normalize -> validate -> persist.

Failure policy
--------------
The pipeline's most important behaviour is what it does when the upstream
source changes shape.  It never persists a result it does not trust:

* extraction confidence below ``SCRAPER_MIN_CONFIDENCE`` aborts persistence;
* zero records aborts persistence;
* a missing identity column aborts persistence.

In each case the existing IPO rows are left exactly as they were, the raw
payload is retained, and the run is recorded as ``FAILED`` with a specific
error code - so a structural change shows up as an observable failure instead
of silently wiping good data.
"""

from __future__ import annotations

import datetime as dt
import time

from app.core.config import settings
from app.core.exceptions import FetchError, ScraperError
from app.core.logging import get_logger
from app.db.enums import ExtractionStrategy, ScrapeStatus
from app.db.models.scrape import ScrapeRun
from app.repositories.ipo import IPORepository
from app.repositories.scrape import ScrapeRepository
from app.services.scraper.client import ScraperHTTPClient, build_report_api_url
from app.services.scraper.extractor import ExtractorChain
from app.services.scraper.models import (
    ExtractionResult,
    RawPayload,
    ScrapeOutcome,
    ValidationReport,
)
from app.services.scraper.normalizer import IPONormalizer
from app.services.scraper.validator import IPOValidator, compute_confidence
from app.utils.dates import today_in_app_timezone, utc_now

logger = get_logger(__name__)

SOURCE_NAME = "investorgain"


class ScrapePipeline:
    """Runs one complete scrape and records the outcome."""

    def __init__(
        self,
        ipo_repository: IPORepository,
        scrape_repository: ScrapeRepository,
        http_client: ScraperHTTPClient | None = None,
        extractor: ExtractorChain | None = None,
    ) -> None:
        self.ipos = ipo_repository
        self.runs = scrape_repository
        self.http = http_client or ScraperHTTPClient()
        self.extractor = extractor or ExtractorChain()

    async def run(self) -> ScrapeOutcome:
        """Execute the pipeline, always returning an outcome (never raising)."""
        started = time.perf_counter()
        today = today_in_app_timezone()
        api_url = build_report_api_url(today=today)
        run = await self.runs.start_run(api_url)

        payload: RawPayload | None = None
        outcome: ScrapeOutcome
        try:
            payload, extraction = await self._fetch_and_extract(api_url)
            outcome = await self._process(run, payload, extraction, today)
        except FetchError as exc:
            outcome = self._failure(exc, http_status=None)
        except ScraperError as exc:
            outcome = self._failure(exc)
        except Exception as exc:
            logger.exception("scraper.unexpected_failure")
            outcome = ScrapeOutcome(
                status=ScrapeStatus.FAILED.value,
                error_code="SCRAPER_UNEXPECTED_ERROR",
                error_message=str(exc),
            )

        outcome.duration_ms = int((time.perf_counter() - started) * 1000)
        if payload is not None:
            outcome.http_status = payload.http_status
            await self._retain_payload(run, payload, outcome)
        await self.runs.complete_run(run, outcome)

        logger.info(
            "scrape.completed",
            extra={
                "status": outcome.status,
                "strategy": outcome.strategy.value,
                "confidence": outcome.confidence,
                "found": outcome.records_found,
                "valid": outcome.records_valid,
                "invalid": outcome.records_invalid,
                "inserted": outcome.persistence.inserted,
                "updated": outcome.persistence.updated,
                "unchanged": outcome.persistence.unchanged,
                "snapshots": outcome.persistence.snapshots,
                "duration_ms": outcome.duration_ms,
                "error_code": outcome.error_code,
            },
        )
        return outcome

    # ------------------------------------------------------------------
    async def _fetch_and_extract(self, api_url: str) -> tuple[RawPayload, ExtractionResult]:
        """Fetch the JSON feed, falling back to the HTML page if it fails.

        The fallback is what makes the scraper survive the feed being moved or
        restructured: the HTML strategy then reads whatever the page itself
        renders.
        """
        async with self.http as client:
            try:
                payload = await client.fetch(api_url)
                extraction = self.extractor.extract(payload)
                if (
                    extraction.succeeded
                    and extraction.confidence >= settings.scraper_min_confidence
                ):
                    return payload, extraction
                logger.warning(
                    "scraper.primary_source_unusable",
                    extra={
                        "confidence": extraction.confidence,
                        "records": len(extraction.records),
                    },
                )
            except FetchError:
                logger.warning("scraper.primary_source_unreachable", extra={"url": api_url})
                payload, extraction = None, None  # type: ignore[assignment]

            fallback = await client.fetch(settings.scraper_page_url)
            fallback_extraction = self.extractor.extract(fallback)

            # Keep whichever attempt understood the data better.
            if extraction is not None and extraction.confidence >= fallback_extraction.confidence:
                return payload, extraction  # type: ignore[return-value]
            fallback_extraction.warnings.insert(0, "primary JSON source unusable; used HTML page")
            return fallback, fallback_extraction

    async def _process(
        self,
        run: ScrapeRun,
        payload: RawPayload,
        extraction: ExtractionResult,
        today: dt.date,
    ) -> ScrapeOutcome:
        """Normalize, validate, gate on confidence, then persist."""
        outcome = ScrapeOutcome(
            status=ScrapeStatus.FAILED.value,
            strategy=extraction.strategy,
            records_found=len(extraction.records),
            warnings=list(extraction.warnings),
            field_mapping=extraction.field_mapping,
            http_status=payload.http_status,
        )
        outcome.warnings.extend(await self._detect_structure_change(extraction))

        if not extraction.records:
            outcome.error_code = "SCRAPER_EXTRACTION_FAILED"
            outcome.error_message = (
                "No IPO records could be located in the upstream response."
            )
            return outcome

        normalizer = IPONormalizer(reference_date=today, base_url="https://www.investorgain.com")
        normalized = normalizer.normalize_many(extraction.records)

        validator = IPOValidator(reference_date=today)
        report: ValidationReport = validator.validate(normalized)
        outcome.records_valid = len(report.valid)
        outcome.records_invalid = len(report.rejected) + (
            len(extraction.records) - len(normalized)
        )
        outcome.confidence = compute_confidence(extraction.confidence, report)
        outcome.warnings.extend(
            f"row {issue.record_index} {issue.field_name}: {issue.message}"
            for issue in report.issues[:50]
        )

        if outcome.confidence < settings.scraper_min_confidence:
            # Refuse to write. Existing IPO data stays untouched.
            outcome.error_code = "SCRAPER_LOW_CONFIDENCE"
            outcome.error_message = (
                f"Extraction confidence {outcome.confidence} is below the configured "
                f"minimum {settings.scraper_min_confidence}; existing IPO data was left "
                "unchanged."
            )
            logger.error(
                "scrape.low_confidence_abort",
                extra={
                    "confidence": outcome.confidence,
                    "threshold": settings.scraper_min_confidence,
                    "valid_records": len(report.valid),
                },
            )
            return outcome

        if not report.valid:
            outcome.error_code = "SCRAPER_NO_VALID_RECORDS"
            outcome.error_message = "Every extracted record failed validation."
            return outcome

        outcome.persistence = await self.ipos.upsert_many(
            report.valid,
            source=SOURCE_NAME,
            scrape_run_id=run.id,
            write_snapshots=settings.scraper_snapshots_enabled,
        )
        outcome.status = (
            ScrapeStatus.PARTIAL.value
            if outcome.records_invalid or outcome.warnings
            else ScrapeStatus.SUCCESS.value
        )
        return outcome

    async def _detect_structure_change(self, extraction: ExtractionResult) -> list[str]:
        """Compare this run's field mapping with the last successful one.

        A field that used to map and no longer does is the earliest signal that
        the upstream page has been restructured, well before it degrades into a
        confidence failure.
        """
        previous = await self.runs.previous_field_mapping()
        if not previous:
            return []

        warnings: list[str] = []
        for field_name, column in previous.items():
            if field_name not in extraction.field_mapping:
                warnings.append(
                    f"structure change: field {field_name!r} (previously column "
                    f"{column!r}) is no longer present"
                )
            elif extraction.field_mapping[field_name] != column:
                warnings.append(
                    f"structure change: field {field_name!r} moved from column "
                    f"{column!r} to {extraction.field_mapping[field_name]!r}"
                )
        if warnings:
            logger.warning(
                "scraper.structure_changed", extra={"changes": len(warnings)}
            )
        return warnings

    async def _retain_payload(
        self, run: ScrapeRun, payload: RawPayload, outcome: ScrapeOutcome
    ) -> None:
        """Store the raw response according to the retention policy."""
        mode = settings.scraper_raw_retention_mode
        failed = outcome.status == ScrapeStatus.FAILED.value
        if mode == "never" or (mode == "on_failure" and not failed):
            return
        await self.runs.store_payload(run, payload)

    @staticmethod
    def _failure(exc: ScraperError, http_status: int | None = None) -> ScrapeOutcome:
        logger.error(
            "scrape.failed",
            extra={"error_code": exc.code, "reason": exc.message},
        )
        return ScrapeOutcome(
            status=ScrapeStatus.FAILED.value,
            strategy=ExtractionStrategy.NONE,
            error_code=exc.code,
            error_message=exc.message,
            http_status=http_status,
        )


async def purge_expired_payloads(scrape_repository: ScrapeRepository) -> int:
    """Delete retained payloads past the retention window."""
    if settings.scraper_raw_retention_days <= 0:
        return 0
    cutoff = utc_now() - dt.timedelta(days=settings.scraper_raw_retention_days)
    removed = await scrape_repository.purge_payloads_older_than(cutoff)
    if removed:
        logger.info("scraper.payloads_purged", extra={"removed": removed})
    return removed
