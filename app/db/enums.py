"""Enumerations shared by ORM models, API schemas and services.

These are stored as PostgreSQL ``VARCHAR`` with a CHECK-style application
constraint rather than native ``ENUM`` types: adding a new member then needs no
``ALTER TYPE`` migration, which matters because IPO types and exchanges are
values the upstream source can extend at any time.
"""

from __future__ import annotations

from enum import StrEnum


class IPOStatus(StrEnum):
    """Lifecycle stage of an IPO.

    Always *derived* from the IPO's dates against the current business date -
    never persisted - so it can never go stale between scrapes.
    """

    UPCOMING = "UPCOMING"
    OPEN = "OPEN"
    CLOSING_TODAY = "CLOSING_TODAY"
    CLOSED = "CLOSED"
    LISTED = "LISTED"
    UNKNOWN = "UNKNOWN"


class IPOType(StrEnum):
    """Board the issue is listed on."""

    MAINBOARD = "MAINBOARD"
    SME = "SME"
    UNKNOWN = "UNKNOWN"


class Exchange(StrEnum):
    """Exchange / trading platform for the issue."""

    NSE = "NSE"
    BSE = "BSE"
    NSE_SME = "NSE_SME"
    BSE_SME = "BSE_SME"
    NSE_BSE = "NSE_BSE"
    UNKNOWN = "UNKNOWN"


class ScrapeStatus(StrEnum):
    """Outcome of a single scraper execution."""

    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"  # data persisted, but with warnings or dropped rows
    FAILED = "FAILED"  # nothing persisted; existing data left untouched


class ExtractionStrategy(StrEnum):
    """Which extraction strategy produced a dataset."""

    JSON_API = "JSON_API"
    HTML_TABLE = "HTML_TABLE"
    NONE = "NONE"


class DeviceType(StrEnum):
    """Platform of a registered push target."""

    ANDROID = "ANDROID"
    IOS = "IOS"
    WEB = "WEB"


class NotificationChannel(StrEnum):
    """Transport used to deliver a notification."""

    PUSH = "PUSH"  # FCM (Android/iOS/Flutter)
    WEBPUSH = "WEBPUSH"  # Browser Web Push
    LOG = "LOG"  # No-op sink used until a real provider is configured


class DeliveryStatus(StrEnum):
    """State of one notification delivery attempt."""

    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"  # eligible but not delivered (e.g. no active device)
