"""Notification preference and delivery data access.

The delivery repository is where deduplication is enforced: a claim is an
``INSERT ... ON CONFLICT DO NOTHING`` against the
``(preference_id, ipo_id, period_key)`` unique constraint.  Whichever worker
wins the insert owns the send; every other caller gets ``None`` and moves on.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import Row, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import DeliveryStatus
from app.db.models.ipo import IPO
from app.db.models.notification import NotificationDelivery, NotificationPreference
from app.db.models.user import User
from app.utils.dates import utc_now


class NotificationPreferenceRepository:
    """CRUD for user-defined notification rules."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_for_user(self, user_id: uuid.UUID) -> list[NotificationPreference]:
        statement = (
            select(NotificationPreference)
            .where(NotificationPreference.user_id == user_id)
            .order_by(NotificationPreference.created_at.asc())
        )
        return list((await self.session.execute(statement)).scalars().all())

    async def get_for_user(
        self, preference_id: uuid.UUID, user_id: uuid.UUID
    ) -> NotificationPreference | None:
        """Fetch scoped to the owner, so one user cannot read another's rule."""
        statement = select(NotificationPreference).where(
            NotificationPreference.id == preference_id,
            NotificationPreference.user_id == user_id,
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def create(self, user_id: uuid.UUID, values: dict[str, Any]) -> NotificationPreference:
        preference = NotificationPreference(user_id=user_id, **values)
        self.session.add(preference)
        await self.session.flush()
        return preference

    async def update(
        self, preference: NotificationPreference, values: dict[str, Any]
    ) -> NotificationPreference:
        for key, value in values.items():
            setattr(preference, key, value)
        await self.session.flush()
        return preference

    async def delete(self, preference: NotificationPreference) -> None:
        await self.session.delete(preference)
        await self.session.flush()

    async def list_active_with_users(self) -> Sequence[Row[tuple[NotificationPreference, User]]]:
        """Every enabled rule belonging to an active user.

        Joined in one query - the worker would otherwise issue a user lookup
        per rule (the classic N+1).
        """
        statement = (
            select(NotificationPreference, User)
            .join(User, User.id == NotificationPreference.user_id)
            .where(NotificationPreference.is_enabled.is_(True), User.is_active.is_(True))
            .order_by(NotificationPreference.created_at.asc())
        )
        return (await self.session.execute(statement)).all()

    async def mark_evaluated(self, preference_ids: Sequence[uuid.UUID]) -> None:
        if not preference_ids:
            return
        now = utc_now()
        statement = select(NotificationPreference).where(
            NotificationPreference.id.in_(preference_ids)
        )
        for preference in (await self.session.execute(statement)).scalars().all():
            preference.last_evaluated_at = now
        await self.session.flush()


class NotificationDeliveryRepository:
    """The delivery ledger, and the idempotency guarantee built on it."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def claim(
        self,
        *,
        user_id: uuid.UUID,
        preference_id: uuid.UUID,
        ipo_id: uuid.UUID,
        channel: str,
        period_key: int,
        business_date: dt.date,
        gmp_percentage: Decimal | None,
        payload: dict[str, Any],
    ) -> uuid.UUID | None:
        """Reserve the right to send this notification.

        Returns the new delivery id, or ``None`` when another worker (or an
        earlier run in the same window) already claimed it.  Because the claim
        is a single atomic statement, concurrent workers cannot both win.
        """
        statement = (
            pg_insert(NotificationDelivery)
            .values(
                user_id=user_id,
                preference_id=preference_id,
                ipo_id=ipo_id,
                channel=channel,
                period_key=period_key,
                business_date=business_date,
                status=DeliveryStatus.PENDING.value,
                gmp_percentage_at_send=gmp_percentage,
                payload=payload,
            )
            .on_conflict_do_nothing(constraint="uq_notification_delivery_period")
            .returning(NotificationDelivery.id)
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def mark_sent(
        self, delivery_id: uuid.UUID, *, provider: str, provider_message_id: str | None
    ) -> None:
        delivery = await self.session.get(NotificationDelivery, delivery_id)
        if delivery is None:
            return
        delivery.status = DeliveryStatus.SENT.value
        delivery.sent_at = utc_now()
        delivery.attempts += 1
        delivery.provider = provider
        delivery.provider_message_id = provider_message_id

    async def mark_failed(
        self, delivery_id: uuid.UUID, *, provider: str, error: str
    ) -> None:
        delivery = await self.session.get(NotificationDelivery, delivery_id)
        if delivery is None:
            return
        delivery.status = DeliveryStatus.FAILED.value
        delivery.failed_at = utc_now()
        delivery.attempts += 1
        delivery.provider = provider
        # Truncated: provider errors can be very long and add no value beyond
        # the first line.
        delivery.error_message = error[:1000]

    async def mark_skipped(self, delivery_id: uuid.UUID, *, reason: str) -> None:
        delivery = await self.session.get(NotificationDelivery, delivery_id)
        if delivery is None:
            return
        delivery.status = DeliveryStatus.SKIPPED.value
        delivery.error_message = reason[:1000]

    async def list_for_user(
        self, user_id: uuid.UUID, *, limit: int, offset: int
    ) -> tuple[list[tuple[NotificationDelivery, str]], int]:
        """Delivery history joined to IPO names, plus the total count."""
        from sqlalchemy import func

        base = select(NotificationDelivery).where(NotificationDelivery.user_id == user_id)
        total = int(
            (
                await self.session.execute(
                    select(func.count()).select_from(base.subquery())
                )
            ).scalar_one()
        )
        statement = (
            select(NotificationDelivery, IPO.name)
            .join(IPO, IPO.id == NotificationDelivery.ipo_id)
            .where(NotificationDelivery.user_id == user_id)
            .order_by(NotificationDelivery.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self.session.execute(statement)).all()
        return [(row[0], row[1]) for row in rows], total
