"""Device (push target) data access."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.device import Device
from app.utils.dates import utc_now


class DeviceRepository:
    """Reads and writes for registered push targets."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_active_for_user(self, user_id: uuid.UUID) -> list[Device]:
        statement = select(Device).where(
            Device.user_id == user_id, Device.is_active.is_(True)
        )
        return list((await self.session.execute(statement)).scalars().all())

    async def list_for_user(self, user_id: uuid.UUID) -> list[Device]:
        statement = (
            select(Device)
            .where(Device.user_id == user_id)
            .order_by(Device.created_at.desc())
        )
        return list((await self.session.execute(statement)).scalars().all())

    async def get_for_user(self, device_id: uuid.UUID, user_id: uuid.UUID) -> Device | None:
        statement = select(Device).where(Device.id == device_id, Device.user_id == user_id)
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def register(
        self,
        *,
        user_id: uuid.UUID,
        device_type: str,
        push_token: str,
        device_name: str | None = None,
        app_version: str | None = None,
    ) -> Device:
        """Register or refresh a push target.

        Push tokens are reassigned by the provider when an app is reinstalled
        or restored onto another device, so an existing token is *moved* to the
        registering user and reactivated rather than rejected as a duplicate.
        """
        existing = (
            await self.session.execute(select(Device).where(Device.push_token == push_token))
        ).scalar_one_or_none()

        now = utc_now()
        if existing is not None:
            existing.user_id = user_id
            existing.device_type = device_type
            existing.device_name = device_name or existing.device_name
            existing.app_version = app_version or existing.app_version
            existing.is_active = True
            existing.invalidated_at = None
            existing.last_seen_at = now
            await self.session.flush()
            return existing

        device = Device(
            user_id=user_id,
            device_type=device_type,
            push_token=push_token,
            device_name=device_name,
            app_version=app_version,
            last_seen_at=now,
        )
        self.session.add(device)
        await self.session.flush()
        return device

    async def deactivate(self, device: Device) -> None:
        device.is_active = False
        await self.session.flush()

    async def invalidate_token(self, push_token: str) -> None:
        """Retire a token the provider reported as permanently invalid."""
        device = (
            await self.session.execute(select(Device).where(Device.push_token == push_token))
        ).scalar_one_or_none()
        if device is not None:
            device.is_active = False
            device.invalidated_at = utc_now()
            await self.session.flush()
