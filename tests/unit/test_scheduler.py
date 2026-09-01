"""Scrape scheduling tests.

The scraper runs a handful of times a day at a random moment inside each
configured window, so the properties that matter are: the right number of jobs,
fire times that land inside their window and never before it, and a different
time on different days.
"""

from __future__ import annotations

import datetime as dt

import pytest
from apscheduler.triggers.cron import CronTrigger

from app.core.config import Settings, settings
from app.workers.scheduler import build_scheduler

IST = settings.timezone


@pytest.fixture
def scraper_scheduled(monkeypatch):
    """Force the scrape schedule on, independent of the ambient environment.

    The test container runs with ``SCRAPER_ENABLED=false`` so no suite ever
    reaches the live source; these tests exercise the scheduler itself and must
    not depend on that.
    """
    monkeypatch.setattr(settings, "scraper_enabled", True)
    monkeypatch.setattr(settings, "notification_enabled", True)
    monkeypatch.setattr(settings, "scraper_schedule_times", ["09:00", "14:00", "20:00"])
    monkeypatch.setattr(settings, "scraper_schedule_jitter_minutes", 30)
    return settings


def fire_times(trigger: CronTrigger, days: int = 14) -> list[dt.datetime]:
    """Collect one fire time per day for ``days`` days."""
    times: list[dt.datetime] = []
    cursor = dt.datetime(2026, 9, 1, 0, 0, tzinfo=IST)
    for _ in range(days):
        nxt = trigger.get_next_fire_time(None, cursor)
        if nxt is None:
            break
        times.append(nxt)
        # Jump past this firing to the next day.
        cursor = nxt.replace(hour=23, minute=59, second=59)
    return times


class TestScheduleConfiguration:
    def test_default_is_three_daily_windows(self):
        defaults = Settings(_env_file=None)
        assert defaults.scraper_schedule_times == ["09:00", "14:00", "20:00"]
        assert defaults.scraper_schedule_jitter_minutes == 30
        assert defaults.uses_scheduled_scrape_times

    def test_windows_parse_to_hour_minute_pairs(self):
        assert Settings(_env_file=None).scrape_windows == [(9, 0), (14, 0), (20, 0)]

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("09:00,14:00,20:00", ["09:00", "14:00", "20:00"]),
            ("9:0, 14:5 ,20:30", ["09:00", "14:05", "20:30"]),
            ("07:15", ["07:15"]),
            ("", []),
        ],
    )
    def test_comma_separated_times_are_parsed_and_normalised(self, raw, expected):
        assert Settings(scraper_schedule_times=raw).scraper_schedule_times == expected

    @pytest.mark.parametrize("raw", ["25:00", "09:60", "nine", "09", "09:xx"])
    def test_invalid_times_are_rejected(self, raw):
        with pytest.raises(ValueError):
            Settings(scraper_schedule_times=raw)

    def test_empty_schedule_falls_back_to_interval_mode(self):
        assert not Settings(scraper_schedule_times="").uses_scheduled_scrape_times


class TestScheduledJobs:
    def test_one_scrape_job_per_window_plus_notifications(self, scraper_scheduled):
        scheduler = build_scheduler()
        job_ids = {job.id for job in scheduler.get_jobs()}
        assert {"scrape_ipos_0", "scrape_ipos_1", "scrape_ipos_2"} <= job_ids
        assert "evaluate_notifications" in job_ids

    async def test_jobs_do_not_stack_up(self, scraper_scheduled):
        """A slow scrape must not run concurrently with its next firing.

        Started paused so the job defaults are actually materialised onto the
        jobs; before ``start()`` they are still pending and carry no defaults.
        ``AsyncIOScheduler`` needs a running loop, hence the async test.
        """
        scheduler = build_scheduler()
        scheduler.start(paused=True)
        try:
            jobs = scheduler.get_jobs()
            assert jobs
            for job in jobs:
                assert job.max_instances == 1
                assert job.coalesce is True
        finally:
            scheduler.shutdown(wait=False)


class TestRandomisedFireTimes:
    """The three windows: 09:00-09:30, 14:00-14:30, 20:00-20:30 IST."""

    @pytest.mark.parametrize(("hour", "minute"), [(9, 0), (14, 0), (20, 0)])
    def test_fire_times_land_inside_the_window(self, hour, minute):
        trigger = CronTrigger(
            hour=hour, minute=minute, second=0, timezone=settings.app_timezone, jitter=30 * 60
        )
        for fired in fire_times(trigger, days=30):
            local = fired.astimezone(IST)
            start = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            end = start + dt.timedelta(minutes=30)
            assert start <= local < end, f"{local} outside {start}-{end}"

    def test_fire_time_is_never_earlier_than_the_window_start(self):
        """APScheduler jitter is forward-only: uniform(0, jitter), not +/-."""
        trigger = CronTrigger(
            hour=9, minute=0, second=0, timezone=settings.app_timezone, jitter=30 * 60
        )
        for fired in fire_times(trigger, days=30):
            local = fired.astimezone(IST)
            assert (local.hour, local.minute) >= (9, 0)

    def test_time_differs_between_days(self):
        """Re-rolled per firing, so the schedule is not perfectly periodic."""
        trigger = CronTrigger(
            hour=9, minute=0, second=0, timezone=settings.app_timezone, jitter=30 * 60
        )
        seconds = {
            fired.astimezone(IST).minute * 60 + fired.astimezone(IST).second
            for fired in fire_times(trigger, days=20)
        }
        assert len(seconds) > 1, "fire time is identical every day - jitter not applied"

    def test_three_runs_per_day(self, scraper_scheduled):
        scheduler = build_scheduler()
        scrape_jobs = [j for j in scheduler.get_jobs() if j.id.startswith("scrape_ipos")]
        assert len(scrape_jobs) == 3

        day_start = dt.datetime(2026, 9, 1, 0, 0, tzinfo=IST)
        day_end = day_start + dt.timedelta(days=1)
        fired_today = [
            nxt
            for job in scrape_jobs
            if (nxt := job.trigger.get_next_fire_time(None, day_start)) is not None
            and nxt < day_end
        ]
        assert len(fired_today) == 3
