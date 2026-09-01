"""Scrape run and raw-payload data access."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import ScrapeStatus
from app.db.models.scrape import ScrapeRawPayload, ScrapeRun
from app.services.scraper.models import RawPayload, ScrapeOutcome
from app.utils.dates import utc_now


class ScrapeRepository:
    """Persistence for scraper observability records."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def start_run(self, source_url: str) -> ScrapeRun:
        """Open a run row up front, so a crash still leaves a trace."""
        run = ScrapeRun(
            status=ScrapeStatus.FAILED.value,
            source_url=source_url,
            started_at=utc_now(),
        )
        self.session.add(run)
        await self.session.flush()
        return run

    async def complete_run(self, run: ScrapeRun, outcome: ScrapeOutcome) -> ScrapeRun:
        """Write the final counters and status for a finished run."""
        finished = utc_now()
        run.finished_at = finished
        run.duration_ms = outcome.duration_ms or int(
            (finished - run.started_at).total_seconds() * 1000
        )
        run.status = outcome.status
        run.strategy = outcome.strategy.value
        run.http_status = outcome.http_status
        run.records_found = outcome.records_found
        run.records_valid = outcome.records_valid
        run.records_invalid = outcome.records_invalid
        run.ipos_inserted = outcome.persistence.inserted
        run.ipos_updated = outcome.persistence.updated
        run.ipos_unchanged = outcome.persistence.unchanged
        run.snapshots_created = outcome.persistence.snapshots
        run.confidence = outcome.confidence
        run.error_code = outcome.error_code
        run.error_message = outcome.error_message[:2000] if outcome.error_message else None
        # Bounded: a badly-changed page can generate a warning per row.
        run.warnings = outcome.warnings[:100]
        run.field_mapping = outcome.field_mapping
        await self.session.flush()
        return run

    async def store_payload(self, run: ScrapeRun, payload: RawPayload) -> ScrapeRawPayload:
        """Retain a raw upstream response for later debugging or replay."""
        stored = ScrapeRawPayload(
            scrape_run_id=run.id,
            captured_at=payload.fetched_at,
            source_url=payload.url,
            content_type=payload.content_type[:64],
            content=payload.content,
            content_hash=payload.content_hash,
            byte_size=payload.byte_size,
        )
        self.session.add(stored)
        await self.session.flush()
        return stored

    async def latest_runs(self, limit: int = 20) -> list[ScrapeRun]:
        statement = select(ScrapeRun).order_by(ScrapeRun.started_at.desc()).limit(limit)
        return list((await self.session.execute(statement)).scalars().all())

    async def last_successful_run(self) -> ScrapeRun | None:
        statement = (
            select(ScrapeRun)
            .where(ScrapeRun.status.in_([ScrapeStatus.SUCCESS.value, ScrapeStatus.PARTIAL.value]))
            .order_by(ScrapeRun.started_at.desc())
            .limit(1)
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def get_payload(self, payload_id: uuid.UUID) -> ScrapeRawPayload | None:
        return await self.session.get(ScrapeRawPayload, payload_id)

    async def purge_payloads_older_than(self, cutoff: dt.datetime) -> int:
        """Enforce the raw-payload retention window. Returns rows removed."""
        result = await self.session.execute(
            delete(ScrapeRawPayload).where(ScrapeRawPayload.captured_at < cutoff)
        )
        return int(result.rowcount or 0)

    async def previous_field_mapping(self) -> dict[str, Any] | None:
        """The field mapping used by the last successful run.

        Diffing against this is how an upstream structure change is detected:
        if a field that used to map suddenly does not, the page has moved.
        """
        run = await self.last_successful_run()
        return run.field_mapping if run else None
