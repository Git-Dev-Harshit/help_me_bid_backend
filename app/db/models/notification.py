"""User notification rules and the delivery ledger.

Deduplication
-------------
``notification_deliveries`` carries a unique constraint on
``(preference_id, ipo_id, period_key)``.  ``period_key`` buckets wall-clock
time into fixed windows of the rule's own interval (see
:func:`app.utils.dates.period_index`), so every worker run inside the same
3-hour window computes an identical key.  The database therefore rejects the
second insert outright - duplicate sends are impossible regardless of how many
workers race, how often the scheduler fires, or whether a transaction is
retried.  No distributed lock or external queue is involved.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.db.models.ipo import IPO
    from app.db.models.user import User


class NotificationPreference(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One user-defined notification rule.

    A user may hold several rules (for example "any IPO above 25% GMP hourly"
    plus "SME issues above 10% GMP every 6 hours").  Frequently-evaluated
    criteria are real columns so the worker can filter in SQL; open-ended
    future criteria go in ``extra_conditions`` so adding one needs no
    migration.
    """

    __tablename__ = "notification_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str | None] = mapped_column(String(80), nullable=True)
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    # --- Core criteria ----------------------------------------------------
    min_gmp_percentage: Mapped[float] = mapped_column(
        Numeric(9, 2), nullable=False, default=0, server_default="0"
    )
    max_gmp_percentage: Mapped[float | None] = mapped_column(Numeric(9, 2), nullable=True)
    interval_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=180, server_default="180"
    )
    # The headline business rule. Leaving it on restricts every notification to
    # the IPO's closing day; turning it off is opt-in per rule.
    only_on_close_date: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    # --- Optional narrowing filters --------------------------------------
    # NULL/empty means "no restriction" for both of these.
    ipo_types: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    exchanges: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    min_subscription_times: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)

    channels: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[\"PUSH\"]'::jsonb")
    )
    # Escape hatch for criteria added after this schema shipped.
    extra_conditions: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    last_evaluated_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[User] = relationship(back_populates="preferences")
    deliveries: Mapped[list[NotificationDelivery]] = relationship(
        back_populates="preference", cascade="all, delete-orphan", lazy="noload"
    )

    __table_args__ = (
        CheckConstraint(
            "min_gmp_percentage >= -100 AND min_gmp_percentage <= 1000",
            name="min_gmp_percentage_range",
        ),
        CheckConstraint(
            "max_gmp_percentage IS NULL OR max_gmp_percentage >= min_gmp_percentage",
            name="gmp_percentage_range_ordered",
        ),
        # 15 minutes floor keeps a misconfigured rule from spamming; 7 days
        # ceiling keeps period_key arithmetic sane.
        CheckConstraint(
            "interval_minutes >= 15 AND interval_minutes <= 10080",
            name="interval_minutes_range",
        ),
        Index(
            "ix_notification_preferences_enabled",
            "user_id",
            postgresql_where=text("is_enabled"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<NotificationPreference id={self.id} user={self.user_id} "
            f"min_gmp={self.min_gmp_percentage} every={self.interval_minutes}m>"
        )


class NotificationDelivery(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single notification that was evaluated as eligible.

    The row is inserted *before* the provider is called.  If the insert is
    rejected by the unique constraint, another worker already owns this
    (rule, IPO, window) and this one backs off - that ordering is what makes
    the pipeline safe to run concurrently.
    """

    __tablename__ = "notification_deliveries"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    preference_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("notification_preferences.id", ondelete="CASCADE"),
        nullable=False,
    )
    ipo_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("ipos.id", ondelete="CASCADE"), nullable=False
    )

    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    # Fixed-width time bucket; see the module docstring.
    period_key: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # The IPO close date this alert was raised for, in APP_TIMEZONE.
    business_date: Mapped[dt.date] = mapped_column(Date, nullable=False)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="PENDING", server_default="PENDING"
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Values that triggered the alert, retained so history stays meaningful
    # after the IPO row moves on.
    gmp_percentage_at_send: Mapped[float | None] = mapped_column(Numeric(9, 2), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped[User] = relationship(back_populates="deliveries")
    preference: Mapped[NotificationPreference] = relationship(back_populates="deliveries")
    ipo: Mapped[IPO] = relationship(lazy="noload")

    __table_args__ = (
        # The deduplication guarantee.
        UniqueConstraint(
            "preference_id", "ipo_id", "period_key", name="uq_notification_delivery_period"
        ),
        Index("ix_notification_deliveries_user_id_created_at", "user_id", "created_at"),
        Index("ix_notification_deliveries_status", "status"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<NotificationDelivery id={self.id} status={self.status} "
            f"ipo={self.ipo_id} period={self.period_key}>"
        )
