"""Notification preference and delivery-history schemas."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db.enums import DeliveryStatus, Exchange, IPOType, NotificationChannel

MIN_INTERVAL_MINUTES = 15
MAX_INTERVAL_MINUTES = 10080  # 7 days

IntervalMinutes = Annotated[
    int,
    Field(
        ge=MIN_INTERVAL_MINUTES,
        le=MAX_INTERVAL_MINUTES,
        description=(
            "Minimum gap between notifications for the same IPO, in minutes. "
            "180 = every 3 hours. Also the deduplication window."
        ),
        examples=[180],
    ),
]

GMPPercentage = Annotated[
    Decimal,
    Field(ge=-100, le=1000, description="GMP as a percentage of the cap price."),
]


class NotificationPreferenceBase(BaseModel):
    """Fields common to creating and updating a rule."""

    label: str | None = Field(
        default=None, max_length=80, examples=["High GMP alerts"],
        description="Optional name to tell your rules apart.",
    )
    is_enabled: bool = Field(default=True, description="Rules are evaluated only when enabled.")
    min_gmp_percentage: GMPPercentage = Field(
        default=Decimal("0"), examples=[15],
        description="Notify only when the IPO's GMP percentage is at or above this.",
    )
    max_gmp_percentage: GMPPercentage | None = Field(
        default=None, description="Optional upper bound on GMP percentage."
    )
    interval_minutes: IntervalMinutes = 180
    only_on_close_date: bool = Field(
        default=True,
        description=(
            "When true (the default), notifications are sent only on the IPO's "
            "closing date in APP_TIMEZONE."
        ),
    )
    ipo_types: list[IPOType] | None = Field(
        default=None, description="Restrict to these IPO types. Omit or null for all."
    )
    exchanges: list[Exchange] | None = Field(
        default=None, description="Restrict to these exchanges. Omit or null for all."
    )
    min_subscription_times: Decimal | None = Field(
        default=None, ge=0, description="Optional minimum overall subscription multiple."
    )
    channels: list[NotificationChannel] = Field(
        default_factory=lambda: [NotificationChannel.PUSH],
        min_length=1,
        description="Delivery channels for this rule.",
    )
    extra_conditions: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Forward-compatible criteria store. Conditions added in future API "
            "versions are accepted here without a schema change."
        ),
    )

    @model_validator(mode="after")
    def _check_bounds(self) -> Self:
        if (
            self.max_gmp_percentage is not None
            and self.max_gmp_percentage < self.min_gmp_percentage
        ):
            raise ValueError("max_gmp_percentage must not be less than min_gmp_percentage")
        for field in ("ipo_types", "exchanges", "channels"):
            values = getattr(self, field)
            if values is not None:
                # Preserve order while removing duplicates.
                setattr(self, field, list(dict.fromkeys(values)))
        return self


class NotificationPreferenceCreate(NotificationPreferenceBase):
    """Payload for creating a rule."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "label": "High GMP closing today",
                "min_gmp_percentage": 15,
                "interval_minutes": 180,
                "only_on_close_date": True,
                "ipo_types": ["SME", "MAINBOARD"],
                "channels": ["PUSH"],
            }
        }
    )


class NotificationPreferenceUpdate(BaseModel):
    """Partial update. Omitted fields keep their current value."""

    label: str | None = Field(default=None, max_length=80)
    is_enabled: bool | None = None
    min_gmp_percentage: GMPPercentage | None = None
    max_gmp_percentage: GMPPercentage | None = None
    interval_minutes: IntervalMinutes | None = None
    only_on_close_date: bool | None = None
    ipo_types: list[IPOType] | None = None
    exchanges: list[Exchange] | None = None
    min_subscription_times: Decimal | None = Field(default=None, ge=0)
    channels: list[NotificationChannel] | None = Field(default=None, min_length=1)
    extra_conditions: dict[str, Any] | None = None


class NotificationPreferenceResponse(NotificationPreferenceBase):
    """A stored rule."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    created_at: dt.datetime
    updated_at: dt.datetime
    last_evaluated_at: dt.datetime | None = None


class NotificationDeliveryResponse(BaseModel):
    """One entry from the delivery ledger."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ipo_id: uuid.UUID
    ipo_name: str | None = Field(
        default=None, description="Denormalised for convenience in history views."
    )
    preference_id: uuid.UUID
    channel: NotificationChannel
    status: DeliveryStatus
    business_date: dt.date = Field(description="The IPO close date this alert was raised for.")
    gmp_percentage_at_send: Decimal | None = None
    sent_at: dt.datetime | None = None
    created_at: dt.datetime
    error_message: str | None = None
