"""Notification rule evaluation, deduplication and delivery."""

from app.services.notifications.engine import (
    EvaluationSummary,
    NotificationEngine,
    build_message,
    rule_matches_ipo,
    within_notification_window,
)
from app.services.notifications.providers import (
    DeviceTarget,
    FCMProvider,
    LogProvider,
    NotificationMessage,
    NotificationProvider,
    SendResult,
    WebPushProvider,
    get_provider,
)

__all__ = [
    "DeviceTarget",
    "EvaluationSummary",
    "FCMProvider",
    "LogProvider",
    "NotificationEngine",
    "NotificationMessage",
    "NotificationProvider",
    "SendResult",
    "WebPushProvider",
    "build_message",
    "get_provider",
    "rule_matches_ipo",
    "within_notification_window",
]
