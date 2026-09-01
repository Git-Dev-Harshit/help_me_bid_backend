"""Background scheduler process.

Runs the periodic scrape and notification jobs on an ``AsyncIOScheduler``.

Why APScheduler rather than Celery or ARQ: this workload is a handful of
cron-like jobs with no fan-out, no task queue and no result backend.
APScheduler needs no broker, so the stack stays at API + worker plus
PostgreSQL.  Concurrency safety - the real reason people reach for a broker -
is handled by PostgreSQL advisory locks in the job entrypoints and by the
delivery ledger's unique constraint, both of which keep working if this process
is replicated.

Scrape scheduling
-----------------
By default the scraper runs three times a day, at a *random* moment inside each
configured half-hour window (09:00-09:30, 14:00-14:30, 20:00-20:30 in
``APP_TIMEZONE``).  The randomness comes from APScheduler's ``jitter``, which
adds ``uniform(0, jitter)`` seconds to each fire time and is re-rolled on every
firing - so the request pattern is not perfectly periodic day to day.

Run with::

    python -m app.workers.scheduler
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import signal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.db.enums import ScrapeStatus
from app.db.session import dispose_engine
from app.workers.notification_worker import run_notifications
from app.workers.scraper_worker import run_scrape

logger = get_logger(__name__)

#: Job id used for the one-shot retry after a failed scrape.
RETRY_JOB_ID = "scrape_ipos_retry"


async def _scrape_job(scheduler: AsyncIOScheduler | None = None, *, is_retry: bool = False) -> None:
    """Scheduled scrape. Never raises - a failure must not kill the scheduler."""
    try:
        outcome = await run_scrape()
    except Exception:
        logger.exception("scheduler.scrape_job_failed")
        outcome = None

    if is_retry or scheduler is None or settings.scraper_failure_retry_minutes <= 0:
        return

    # `None` means another worker held the lock, which is not a failure.
    failed = outcome is None or outcome.status == ScrapeStatus.FAILED.value
    if outcome is not None and failed:
        _schedule_retry(scheduler)


def _schedule_retry(scheduler: AsyncIOScheduler) -> None:
    """Queue a single retry after a failed run.

    With only three scrapes a day, one failure would otherwise leave the data
    stale for hours. One retry is enough to cover a transient upstream blip
    without turning into a hot loop against a genuinely broken source.
    """
    run_at = dt.datetime.now(settings.timezone) + dt.timedelta(
        minutes=settings.scraper_failure_retry_minutes
    )
    scheduler.add_job(
        _scrape_job,
        trigger=DateTrigger(run_date=run_at),
        id=RETRY_JOB_ID,
        name="Retry failed IPO scrape",
        kwargs={"scheduler": None, "is_retry": True},
        replace_existing=True,
    )
    logger.warning(
        "scheduler.scrape_retry_scheduled",
        extra={
            "run_at": run_at.isoformat(timespec="seconds"),
            "delay_minutes": settings.scraper_failure_retry_minutes,
        },
    )


async def _database_has_no_ipos() -> bool:
    """True when the IPO table is empty, i.e. this is a fresh install."""
    from sqlalchemy import func, select

    from app.db.models.ipo import IPO
    from app.db.session import SessionFactory

    try:
        async with SessionFactory() as session:
            total = (
                await session.execute(select(func.count()).select_from(IPO))
            ).scalar_one()
        return int(total) == 0
    except Exception:
        # Never let this check stop the worker from starting.
        logger.warning("worker.ipo_count_check_failed", exc_info=True)
        return False


async def _notification_job() -> None:
    """Scheduled notification evaluation."""
    try:
        await run_notifications()
    except Exception:
        logger.exception("scheduler.notification_job_failed")


def _register_scrape_jobs(scheduler: AsyncIOScheduler) -> None:
    """Register the scrape schedule: fixed daily windows, or a fixed interval."""
    if not settings.uses_scheduled_scrape_times:
        scheduler.add_job(
            _scrape_job,
            trigger=IntervalTrigger(minutes=settings.scraper_interval_minutes),
            id="scrape_ipos",
            name="Scrape IPO/GMP data",
            kwargs={"scheduler": scheduler},
        )
        logger.info(
            "scheduler.job_registered",
            extra={
                "job": "scrape_ipos",
                "mode": "interval",
                "interval_minutes": settings.scraper_interval_minutes,
            },
        )
        return

    jitter_seconds = settings.scraper_schedule_jitter_minutes * 60
    for index, (hour, minute) in enumerate(settings.scrape_windows):
        scheduler.add_job(
            _scrape_job,
            trigger=CronTrigger(
                hour=hour,
                minute=minute,
                second=0,
                timezone=settings.app_timezone,
                # Forward-only: uniform(0, jitter). The fire time therefore
                # lands inside [start, start + jitter), never before it.
                jitter=jitter_seconds or None,
            ),
            id=f"scrape_ipos_{index}",
            name=f"Scrape IPO/GMP data ({hour:02d}:{minute:02d} window)",
            kwargs={"scheduler": scheduler},
        )
        window_end = (
            dt.datetime(2000, 1, 1, hour, minute)
            + dt.timedelta(minutes=settings.scraper_schedule_jitter_minutes)
        ).strftime("%H:%M")
        logger.info(
            "scheduler.job_registered",
            extra={
                "job": f"scrape_ipos_{index}",
                "mode": "daily_window",
                "window": f"{hour:02d}:{minute:02d}-{window_end}",
                "timezone": settings.app_timezone,
            },
        )


def build_scheduler() -> AsyncIOScheduler:
    """Create the scheduler with the configured jobs enabled."""
    scheduler = AsyncIOScheduler(
        timezone=settings.app_timezone,
        job_defaults={
            # A slow run must not stack up behind itself.
            "coalesce": True,
            "max_instances": 1,
            # Tolerate brief event-loop delays before skipping a fire.
            "misfire_grace_time": 300,
        },
    )

    if settings.scraper_enabled:
        _register_scrape_jobs(scheduler)
    else:
        logger.warning("scheduler.scraper_disabled")

    if settings.notification_enabled:
        scheduler.add_job(
            _notification_job,
            trigger=IntervalTrigger(minutes=settings.notification_interval_minutes),
            id="evaluate_notifications",
            name="Evaluate notification rules",
        )
        logger.info(
            "scheduler.job_registered",
            extra={
                "job": "evaluate_notifications",
                "interval_minutes": settings.notification_interval_minutes,
            },
        )
    else:
        logger.warning("scheduler.notifications_disabled")

    return scheduler


async def main() -> None:
    """Start the scheduler and run until signalled to stop."""
    configure_logging()
    logger.info(
        "worker.starting",
        extra={
            "environment": settings.app_env,
            "timezone": settings.app_timezone,
            "scraper_enabled": settings.scraper_enabled,
            "scrape_times": ",".join(settings.scraper_schedule_times) or "interval",
            "notifications_enabled": settings.notification_enabled,
        },
    )

    scheduler = build_scheduler()
    scheduler.start()

    startup_tasks: set[asyncio.Task[None]] = set()
    if settings.scraper_enabled and settings.scraper_run_on_startup_if_empty:
        if await _database_has_no_ipos():
            logger.info("worker.initial_scrape_queued", extra={"reason": "no IPO data yet"})
            # Reference held for the task's lifetime: a bare create_task() can
            # be garbage collected while still running.
            task = asyncio.create_task(_scrape_job(scheduler))
            startup_tasks.add(task)
            task.add_done_callback(startup_tasks.discard)
        else:
            logger.info("worker.initial_scrape_skipped", extra={"reason": "IPO data present"})

    for job in scheduler.get_jobs():
        logger.info(
            "scheduler.next_run",
            extra={
                "job": job.id,
                "next_run_time": job.next_run_time.isoformat(timespec="seconds")
                if job.next_run_time
                else None,
            },
        )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):  # not available on Windows
            loop.add_signal_handler(sig, stop.set)

    try:
        await stop.wait()
    finally:
        logger.info("worker.stopping")
        # wait=True lets an in-flight scrape finish rather than being cut off
        # mid-transaction.
        scheduler.shutdown(wait=True)
        await dispose_engine()
        logger.info("worker.stopped")


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
