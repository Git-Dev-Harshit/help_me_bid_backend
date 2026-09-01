"""Run a single background job on demand, then exit.

Useful for debugging, for a first data load, and for driving the jobs from an
external scheduler (cron, Kubernetes CronJob) instead of the bundled worker::

    docker compose exec worker python -m app.workers.run_once scrape
    docker compose exec worker python -m app.workers.run_once notify
    docker compose exec worker python -m app.workers.run_once schedule

Both jobs take the same advisory lock the scheduler uses, so running one by
hand while the worker is live is safe.
"""

from __future__ import annotations

import asyncio
import sys

from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.db.session import dispose_engine
from app.workers.notification_worker import run_notifications
from app.workers.scraper_worker import run_scrape

logger = get_logger(__name__)

JOBS = {"scrape": run_scrape, "notify": run_notifications}
COMMANDS = (*JOBS, "schedule")


def _print_schedule() -> int:
    """Show when each job would next fire, without starting the scheduler."""
    from app.workers.scheduler import build_scheduler

    scheduler = build_scheduler()
    print(f"Timezone: {settings.app_timezone}")
    if settings.uses_scheduled_scrape_times:
        jitter = settings.scraper_schedule_jitter_minutes
        print(f"Scrape windows (random moment within each, re-rolled daily, +{jitter}m):")
        for hour, minute in settings.scrape_windows:
            end_minute = minute + jitter
            end = f"{(hour + end_minute // 60) % 24:02d}:{end_minute % 60:02d}"
            print(f"  {hour:02d}:{minute:02d}-{end}")
    else:
        print(f"Scrape interval: every {settings.scraper_interval_minutes} minutes")

    print("\nNext fire times:")
    # next_run_time is only populated once the scheduler starts, so ask each
    # trigger directly instead.
    import datetime as dt

    now = dt.datetime.now(settings.timezone)
    for job in scheduler.get_jobs():
        nxt = job.trigger.get_next_fire_time(None, now)
        print(f"  {job.id:24} {nxt.isoformat(timespec='seconds') if nxt else 'never'}")
    return 0


async def _run_job(job_name: str) -> int:
    job = JOBS[job_name]
    result = await job()
    if result is None:
        logger.warning("run_once.skipped", extra={"job": job_name, "reason": "lock held"})
    else:
        logger.info("run_once.completed", extra={"job": job_name, "result": str(result)})
    await dispose_engine()
    return 0


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(f"usage: python -m app.workers.run_once [{'|'.join(COMMANDS)}]", file=sys.stderr)
        return 2

    configure_logging()
    command = sys.argv[1]
    if command == "schedule":
        return _print_schedule()
    return asyncio.run(_run_job(command))


if __name__ == "__main__":
    raise SystemExit(main())
