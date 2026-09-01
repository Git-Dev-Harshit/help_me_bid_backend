"""Notification rule evaluation and dispatch.

Eligibility
-----------
A notification is sent only when **all** of the following hold:

1. the rule is enabled and belongs to an active user;
2. ``IPO.close_date == today`` in ``APP_TIMEZONE`` (unless the rule explicitly
   opts out via ``only_on_close_date = false``);
3. ``IPO.gmp_percentage >= rule.min_gmp_percentage`` (and ``<= max`` when set);
4. the current time falls inside the configured quiet-hours window;
5. the rule's interval has elapsed since the last notification for that IPO;
6. no delivery already exists for this (rule, IPO, interval window).

Rules 5 and 6 are the same mechanism: wall-clock time is bucketed into
fixed windows of the rule's interval, and the delivery ledger's unique
constraint rejects a second insert for the same bucket.  That makes the
worker idempotent under concurrency, restarts and scheduler overlap without
any lock or external queue.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.enums import DeviceType, NotificationChannel
from app.db.models.ipo import IPO
from app.db.models.notification import NotificationPreference
from app.db.models.user import User
from app.repositories.device import DeviceRepository
from app.repositories.ipo import IPORepository
from app.repositories.notification import (
    NotificationDeliveryRepository,
    NotificationPreferenceRepository,
)
from app.services.notifications.providers import (
    DeviceTarget,
    NotificationMessage,
    NotificationProvider,
    get_provider,
)
from app.utils.dates import (
    current_hour_in_app_timezone,
    period_index,
    today_in_app_timezone,
    utc_now,
)

logger = get_logger(__name__)


@dataclass(slots=True)
class EvaluationSummary:
    """Counters describing one evaluation pass, for logs and tests."""

    rules_evaluated: int = 0
    ipos_considered: int = 0
    matches: int = 0
    claimed: int = 0
    duplicates_skipped: int = 0
    sent: int = 0
    failed: int = 0
    skipped_no_device: int = 0


def rule_matches_ipo(
    preference: NotificationPreference, ipo: IPO, today: dt.date
) -> bool:
    """Pure predicate: does this IPO satisfy this rule's criteria today?

    Kept free of I/O so the business rules can be unit-tested directly.
    """
    # --- The headline rule: closing-date restriction --------------------
    if preference.only_on_close_date and (
        ipo.close_date is None or ipo.close_date != today
    ):
        return False

    # --- GMP threshold ---------------------------------------------------
    if ipo.gmp_percentage is None:
        return False
    gmp_percentage = Decimal(str(ipo.gmp_percentage))
    if gmp_percentage < Decimal(str(preference.min_gmp_percentage)):
        return False
    if (
        preference.max_gmp_percentage is not None
        and gmp_percentage > Decimal(str(preference.max_gmp_percentage))
    ):
        return False

    # --- Optional narrowing filters --------------------------------------
    if preference.ipo_types and ipo.ipo_type not in preference.ipo_types:
        return False
    if preference.exchanges and ipo.exchange not in preference.exchanges:
        return False
    if preference.min_subscription_times is not None:
        if ipo.subscription_times is None:
            return False
        if Decimal(str(ipo.subscription_times)) < Decimal(
            str(preference.min_subscription_times)
        ):
            return False
    return True


def within_notification_window(now: dt.datetime | None = None) -> bool:
    """True when the current business-timezone hour is inside quiet hours."""
    hour = current_hour_in_app_timezone(now)
    return settings.notification_window_start_hour <= hour < settings.notification_window_end_hour


def build_message(ipo: IPO, preference: NotificationPreference) -> NotificationMessage:
    """Compose the user-facing notification text."""
    gmp_percentage = ipo.gmp_percentage or 0
    title = f"{ipo.name} closes today"
    parts = [f"GMP {gmp_percentage}%"]
    if ipo.gmp is not None:
        parts.append(f"(₹{ipo.gmp})")
    if ipo.price_max is not None:
        parts.append(f"· Price ₹{ipo.price_max}")
    if ipo.subscription_times is not None:
        parts.append(f"· Subscribed {ipo.subscription_times}x")
    body = " ".join(parts)

    return NotificationMessage(
        title=title,
        body=body,
        # All values must be strings for FCM.
        data={
            "ipo_id": str(ipo.id),
            "ipo_name": ipo.name,
            "gmp_percentage": str(gmp_percentage),
            "close_date": ipo.close_date.isoformat() if ipo.close_date else "",
            "preference_id": str(preference.id),
            "type": "ipo_gmp_alert",
        },
    )


class NotificationEngine:
    """Evaluates every enabled rule and dispatches eligible notifications."""

    def __init__(
        self, session: AsyncSession, provider: NotificationProvider | None = None
    ) -> None:
        self.session = session
        self.preferences = NotificationPreferenceRepository(session)
        self.deliveries = NotificationDeliveryRepository(session)
        self.devices = DeviceRepository(session)
        self.ipos = IPORepository(session)
        self.provider = provider or get_provider()

    async def evaluate(self, now: dt.datetime | None = None) -> EvaluationSummary:
        """Run one evaluation pass over all enabled rules."""
        summary = EvaluationSummary()
        now = now or utc_now()

        if not settings.notification_enabled:
            logger.info("notification.evaluation_disabled")
            return summary

        if not within_notification_window(now):
            logger.info(
                "notification.outside_window",
                extra={
                    "hour": current_hour_in_app_timezone(now),
                    "window_start": settings.notification_window_start_hour,
                    "window_end": settings.notification_window_end_hour,
                },
            )
            return summary

        today = today_in_app_timezone(now)
        rules = await self.preferences.list_active_with_users()
        summary.rules_evaluated = len(rules)
        if not rules:
            return summary

        # Load the day's candidate IPOs once and reuse them across every rule,
        # rather than querying per rule.
        closing_today = await self.ipos.list_closing_on(today)
        summary.ipos_considered = len(closing_today)

        evaluated_ids = []
        for preference, user in rules:
            evaluated_ids.append(preference.id)
            candidates = (
                closing_today
                if preference.only_on_close_date
                else await self.ipos.list_closing_on(today)
            )
            for ipo in candidates:
                if not rule_matches_ipo(preference, ipo, today):
                    continue
                summary.matches += 1
                await self._dispatch(preference, user, ipo, today, now, summary)

        await self.preferences.mark_evaluated(evaluated_ids)
        await self.session.commit()

        logger.info(
            "notification.evaluation_completed",
            extra={
                "rules": summary.rules_evaluated,
                "ipos": summary.ipos_considered,
                "matches": summary.matches,
                "claimed": summary.claimed,
                "duplicates_skipped": summary.duplicates_skipped,
                "sent": summary.sent,
                "failed": summary.failed,
                "skipped_no_device": summary.skipped_no_device,
            },
        )
        return summary

    # ------------------------------------------------------------------
    async def _dispatch(
        self,
        preference: NotificationPreference,
        user: User,
        ipo: IPO,
        today: dt.date,
        now: dt.datetime,
        summary: EvaluationSummary,
    ) -> None:
        """Claim the send window, then deliver.

        The claim is written and committed before the provider is called, so a
        crash mid-send cannot produce a duplicate on the next run.
        """
        period_key = period_index(now, preference.interval_minutes)
        channel = self._primary_channel(preference)
        message = build_message(ipo, preference)

        delivery_id = await self.deliveries.claim(
            user_id=user.id,
            preference_id=preference.id,
            ipo_id=ipo.id,
            channel=channel.value,
            period_key=period_key,
            business_date=ipo.close_date or today,
            gmp_percentage=Decimal(str(ipo.gmp_percentage)) if ipo.gmp_percentage else None,
            payload={"title": message.title, "body": message.body},
        )
        if delivery_id is None:
            # Already notified for this rule/IPO/window.
            summary.duplicates_skipped += 1
            return
        summary.claimed += 1
        await self.session.commit()

        targets = await self._targets_for(user, channel)
        if not targets:
            await self.deliveries.mark_skipped(
                delivery_id, reason="user has no active device registered for this channel"
            )
            summary.skipped_no_device += 1
            await self.session.commit()
            return

        try:
            result = await self.provider.send(message, targets)
        except Exception as exc:
            logger.warning(
                "notification.send_failed",
                extra={"delivery_id": str(delivery_id), "reason": type(exc).__name__},
                exc_info=True,
            )
            await self.deliveries.mark_failed(
                delivery_id, provider=self.provider.name, error=str(exc)
            )
            summary.failed += 1
            await self.session.commit()
            return

        for token in result.invalid_tokens:
            await self.devices.invalidate_token(token)

        if result.success:
            await self.deliveries.mark_sent(
                delivery_id,
                provider=result.provider,
                provider_message_id=result.provider_message_id,
            )
            summary.sent += 1
        else:
            await self.deliveries.mark_failed(
                delivery_id,
                provider=result.provider,
                error=result.error or "unknown provider error",
            )
            summary.failed += 1
        await self.session.commit()

    @staticmethod
    def _primary_channel(preference: NotificationPreference) -> NotificationChannel:
        """Pick the channel to record for this delivery."""
        for raw in preference.channels or []:
            try:
                return NotificationChannel(raw)
            except ValueError:
                continue
        return NotificationChannel.PUSH

    async def _targets_for(
        self, user: User, channel: NotificationChannel
    ) -> list[DeviceTarget]:
        """Active devices for a user, restricted to the channel's platforms."""
        devices = await self.devices.list_active_for_user(user.id)
        wanted = (
            {DeviceType.WEB}
            if channel is NotificationChannel.WEBPUSH
            else {DeviceType.ANDROID, DeviceType.IOS}
        )
        targets: list[DeviceTarget] = []
        for device in devices:
            try:
                device_type = DeviceType(device.device_type)
            except ValueError:
                continue
            # The log provider accepts anything: it is a sink, not a transport.
            if channel is not NotificationChannel.LOG and device_type not in wanted:
                continue
            targets.append(
                DeviceTarget(
                    device_id=str(device.id),
                    device_type=device_type,
                    push_token=device.push_token,
                )
            )
        return targets
