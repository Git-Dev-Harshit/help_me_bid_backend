"""Notification evaluation job entrypoint.

Like the scraper, guarded by a PostgreSQL advisory lock so overlapping runs
cannot both evaluate the same rules.  Even without the lock the delivery
ledger's unique constraint would prevent duplicate *sends* - the lock simply
saves the wasted work.
"""

from __future__ import annotations

from sqlalchemy import text

from app.core.logging import get_logger
from app.db.session import session_scope
from app.services.notifications.engine import EvaluationSummary, NotificationEngine

logger = get_logger(__name__)

NOTIFICATION_LOCK_KEY = 8_412_002


async def run_notifications() -> EvaluationSummary | None:
    """Evaluate all rules once, or return ``None`` if another worker is running."""
    async with session_scope() as session:
        acquired = bool(
            (
                await session.execute(
                    text("SELECT pg_try_advisory_lock(:key)"),
                    {"key": NOTIFICATION_LOCK_KEY},
                )
            ).scalar()
        )
        if not acquired:
            logger.info("notification.skipped_lock_held")
            return None

        try:
            return await NotificationEngine(session).evaluate()
        finally:
            await session.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": NOTIFICATION_LOCK_KEY}
            )
