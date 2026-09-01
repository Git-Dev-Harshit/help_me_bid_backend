"""Timezone helpers.

Two rules hold everywhere in this codebase:

1. Every timestamp persisted to PostgreSQL is timezone-aware UTC.
2. Every *business date* decision ("is this IPO closing today?") is made in
   ``APP_TIMEZONE``, never in the server's local timezone.

Mixing the two is the classic source of "the notification fired a day early"
bugs, so the conversion is centralised here.
"""

from __future__ import annotations

import datetime as dt

from app.core.config import settings


def utc_now() -> dt.datetime:
    """Current instant as an aware UTC datetime."""
    return dt.datetime.now(dt.UTC)


def to_utc(value: dt.datetime) -> dt.datetime:
    """Convert an aware datetime to UTC; assume UTC for naive input."""
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def to_app_timezone(value: dt.datetime) -> dt.datetime:
    """Convert an instant into the configured business timezone."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(settings.timezone)


def today_in_app_timezone(now: dt.datetime | None = None) -> dt.date:
    """The current business date in ``APP_TIMEZONE``.

    This is the date used for the "IPO closes today" notification rule.
    """
    return to_app_timezone(now or utc_now()).date()


def current_hour_in_app_timezone(now: dt.datetime | None = None) -> int:
    """Hour of day (0-23) in the business timezone, for quiet-hours checks."""
    return to_app_timezone(now or utc_now()).hour


def start_of_day_utc(day: dt.date) -> dt.datetime:
    """UTC instant at which ``day`` begins in the business timezone."""
    local = dt.datetime.combine(day, dt.time.min, tzinfo=settings.timezone)
    return local.astimezone(dt.UTC)


def end_of_day_utc(day: dt.date) -> dt.datetime:
    """UTC instant at which ``day`` ends (exclusive) in the business timezone."""
    return start_of_day_utc(day + dt.timedelta(days=1))


def period_index(now: dt.datetime, interval_minutes: int) -> int:
    """Bucket an instant into a fixed-width interval since the Unix epoch.

    This is the idempotency key for repeat notifications: within a single
    3-hour window every worker run computes the same index, so a unique
    constraint on ``(preference, ipo, period)`` collapses duplicate sends no
    matter how many workers race or how often the scheduler fires.
    """
    if interval_minutes < 1:
        raise ValueError("interval_minutes must be >= 1")
    return int(to_utc(now).timestamp()) // (interval_minutes * 60)
