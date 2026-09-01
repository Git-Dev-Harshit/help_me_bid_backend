"""Application services for notification preferences, devices and history."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    DeviceNotFoundError,
    NotificationPreferenceNotFoundError,
)
from app.core.logging import get_logger
from app.db.models.notification import NotificationPreference
from app.repositories.device import DeviceRepository
from app.repositories.notification import (
    NotificationDeliveryRepository,
    NotificationPreferenceRepository,
)
from app.schemas.common import Page, PaginationMeta
from app.schemas.device import DeviceRegisterRequest, DeviceResponse
from app.schemas.notification import (
    NotificationDeliveryResponse,
    NotificationPreferenceCreate,
    NotificationPreferenceResponse,
    NotificationPreferenceUpdate,
)

logger = get_logger(__name__)


def _to_storage(payload: NotificationPreferenceCreate | NotificationPreferenceUpdate,
                *, partial: bool) -> dict[str, object]:
    """Convert a request model into column values.

    Enum lists are stored as plain strings in JSONB so the column stays
    readable and no database enum type has to be altered when a new IPO type or
    exchange appears upstream.
    """
    values = payload.model_dump(exclude_unset=partial, exclude_none=False)
    for key in ("ipo_types", "exchanges", "channels"):
        if key in values and values[key] is not None:
            values[key] = [str(item) for item in values[key]]
    return values


class NotificationPreferenceService:
    """CRUD for a user's notification rules."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repository = NotificationPreferenceRepository(session)

    async def list_for_user(self, user_id: uuid.UUID) -> list[NotificationPreferenceResponse]:
        preferences = await self.repository.list_for_user(user_id)
        return [NotificationPreferenceResponse.model_validate(p) for p in preferences]

    async def get(
        self, preference_id: uuid.UUID, user_id: uuid.UUID
    ) -> NotificationPreferenceResponse:
        preference = await self._get_owned(preference_id, user_id)
        return NotificationPreferenceResponse.model_validate(preference)

    async def create(
        self, user_id: uuid.UUID, payload: NotificationPreferenceCreate
    ) -> NotificationPreferenceResponse:
        preference = await self.repository.create(user_id, _to_storage(payload, partial=False))
        await self.session.commit()
        logger.info(
            "notification.preference_created",
            extra={
                "user_id": str(user_id),
                "preference_id": str(preference.id),
                "min_gmp_percentage": str(preference.min_gmp_percentage),
                "interval_minutes": preference.interval_minutes,
            },
        )
        return NotificationPreferenceResponse.model_validate(preference)

    async def update(
        self,
        preference_id: uuid.UUID,
        user_id: uuid.UUID,
        payload: NotificationPreferenceUpdate,
    ) -> NotificationPreferenceResponse:
        preference = await self._get_owned(preference_id, user_id)
        updated = await self.repository.update(preference, _to_storage(payload, partial=True))
        await self.session.commit()
        return NotificationPreferenceResponse.model_validate(updated)

    async def delete(self, preference_id: uuid.UUID, user_id: uuid.UUID) -> None:
        preference = await self._get_owned(preference_id, user_id)
        await self.repository.delete(preference)
        await self.session.commit()

    async def _get_owned(
        self, preference_id: uuid.UUID, user_id: uuid.UUID
    ) -> NotificationPreference:
        """Load a rule, or 404 if it does not belong to this user.

        Returning "not found" rather than "forbidden" for another user's rule
        avoids confirming that the id exists.
        """
        preference = await self.repository.get_for_user(preference_id, user_id)
        if preference is None:
            raise NotificationPreferenceNotFoundError()
        return preference


class DeviceService:
    """Registration and lifecycle for push targets."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repository = DeviceRepository(session)

    async def register(
        self, user_id: uuid.UUID, payload: DeviceRegisterRequest
    ) -> DeviceResponse:
        device = await self.repository.register(
            user_id=user_id,
            device_type=payload.device_type.value,
            push_token=payload.push_token,
            device_name=payload.device_name,
            app_version=payload.app_version,
        )
        await self.session.commit()
        logger.info(
            "device.registered",
            extra={
                "user_id": str(user_id),
                "device_id": str(device.id),
                "device_type": device.device_type,
            },
        )
        return DeviceResponse.model_validate(device)

    async def list_for_user(self, user_id: uuid.UUID) -> list[DeviceResponse]:
        devices = await self.repository.list_for_user(user_id)
        return [DeviceResponse.model_validate(device) for device in devices]

    async def deregister(self, device_id: uuid.UUID, user_id: uuid.UUID) -> None:
        device = await self.repository.get_for_user(device_id, user_id)
        if device is None:
            raise DeviceNotFoundError()
        await self.repository.deactivate(device)
        await self.session.commit()


class NotificationHistoryService:
    """Read access to a user's delivery ledger."""

    def __init__(self, session: AsyncSession) -> None:
        self.repository = NotificationDeliveryRepository(session)

    async def list_for_user(
        self, user_id: uuid.UUID, *, page: int, page_size: int
    ) -> Page[NotificationDeliveryResponse]:
        rows, total = await self.repository.list_for_user(
            user_id, limit=page_size, offset=(page - 1) * page_size
        )
        items = []
        for delivery, ipo_name in rows:
            response = NotificationDeliveryResponse.model_validate(delivery)
            response.ipo_name = ipo_name
            items.append(response)
        return Page[NotificationDeliveryResponse](
            items=items,
            pagination=PaginationMeta.build(
                page=page, page_size=page_size, total_items=total
            ),
        )
