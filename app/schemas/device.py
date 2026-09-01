"""Device registration schemas."""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.db.enums import DeviceType


class DeviceRegisterRequest(BaseModel):
    """Register (or re-register) a push target.

    Registering an existing ``push_token`` is idempotent: the token is
    reassigned to the calling user and reactivated rather than duplicated,
    which is what FCM token rotation requires.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "device_type": "ANDROID",
                "push_token": "fcm-token-issued-by-firebase",
                "device_name": "Pixel 8",
                "app_version": "1.0.0",
            }
        }
    )

    device_type: DeviceType
    push_token: str = Field(
        min_length=8,
        max_length=4096,
        description="Provider-issued token. Never logged.",
    )
    device_name: str | None = Field(default=None, max_length=120)
    app_version: str | None = Field(default=None, max_length=40)


class DeviceResponse(BaseModel):
    """A registered device. The push token itself is deliberately not returned."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    device_type: DeviceType
    device_name: str | None = None
    app_version: str | None = None
    is_active: bool
    created_at: dt.datetime
    last_seen_at: dt.datetime | None = None
