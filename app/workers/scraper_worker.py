"""Scraper job entrypoint.

Wrapped in a PostgreSQL advisory lock so that running several worker replicas
(or a scheduler that overlaps a slow run) still results in exactly one scrape
at a time.  The lock is session-scoped and released automatically if the
process dies, so a crash cannot wedge the job permanently - which is the main
reason to prefer it over a lock row or an external queue.
"""

from __future__ import annotations

from sqlalchemy import text

from app.core.logging import get_logger
from app.db.session import session_scope
from app.repositories.ipo import IPORepository
from app.repositories.scrape import ScrapeRepository
from app.services.scraper.models import ScrapeOutcome
from app.services.scraper.pipeline import ScrapePipeline, purge_expired_payloads

logger = get_logger(__name__)

#: Arbitrary but stable key identifying the scrape job's advisory lock.
SCRAPE_LOCK_KEY = 8_412_001


async def run_scrape() -> ScrapeOutcome | None:
    """Run one scrape, or return ``None`` when another worker holds the lock."""
    async with session_scope() as session:
        acquired = bool(
            (
                await session.execute(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": SCRAPE_LOCK_KEY}
                )
            ).scalar()
        )
        if not acquired:
            logger.info("scrape.skipped_lock_held")
            return None

        try:
            pipeline = ScrapePipeline(
                ipo_repository=IPORepository(session),
                scrape_repository=ScrapeRepository(session),
            )
            outcome = await pipeline.run()
            await purge_expired_payloads(ScrapeRepository(session))
            return outcome
        finally:
            await session.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": SCRAPE_LOCK_KEY}
            )
