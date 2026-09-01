"""Notification preference, device and delivery-history endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, status

from app.api.deps import CurrentUser, DbSession, Pagination
from app.schemas.common import ErrorResponse, Page
from app.schemas.device import DeviceRegisterRequest, DeviceResponse
from app.schemas.notification import (
    NotificationDeliveryResponse,
    NotificationPreferenceCreate,
    NotificationPreferenceResponse,
    NotificationPreferenceUpdate,
)
from app.services.notifications.service import (
    DeviceService,
    NotificationHistoryService,
    NotificationPreferenceService,
)

# No default tags: each route declares its own OpenAPI group below.
router = APIRouter()

_AUTH_RESPONSES: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse, "description": "Missing, invalid or expired token."},
    403: {"model": ErrorResponse, "description": "Account is deactivated."},
}
_NOT_FOUND: dict[int | str, dict[str, object]] = {
    404: {"model": ErrorResponse, "description": "Preference not found."},
}

PREFERENCE_RULE_DOC = (
    "A notification is sent only when **all** of these hold:\n\n"
    "1. `IPO.close_date` equals today in `APP_TIMEZONE` "
    "(unless `only_on_close_date` is `false`);\n"
    "2. the IPO's GMP percentage is at or above `min_gmp_percentage` "
    "(and at or below `max_gmp_percentage` when set);\n"
    "3. the current time is inside the server's notification window;\n"
    "4. `interval_minutes` has elapsed since the last alert for that IPO;\n"
    "5. no notification has already been sent for that interval.\n\n"
    "Deduplication is enforced by a database constraint, so repeated worker "
    "runs, restarts or several workers in parallel cannot produce duplicates."
)


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------
@router.get(
    "/notification-preferences",
    response_model=list[NotificationPreferenceResponse],
    summary="List your notification rules",
    description="Returns every notification rule belonging to the authenticated user.",
    responses=_AUTH_RESPONSES,
    tags=["Notification Preferences"],
)
async def list_preferences(
    user: CurrentUser, session: DbSession
) -> list[NotificationPreferenceResponse]:
    return await NotificationPreferenceService(session).list_for_user(user.id)


@router.post(
    "/notification-preferences",
    response_model=NotificationPreferenceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a notification rule",
    description=(
        "Creates a rule describing when to be notified about an IPO.\n\n"
        f"{PREFERENCE_RULE_DOC}\n\n"
        "**Example** - *notify me every 3 hours about IPOs closing today whose "
        "GMP is at least 15%*:\n\n"
        "```json\n"
        '{"min_gmp_percentage": 15, "interval_minutes": 180, '
        '"only_on_close_date": true}\n'
        "```\n\n"
        "A user may hold several rules; each is evaluated independently."
    ),
    responses={**_AUTH_RESPONSES, 422: {"model": ErrorResponse, "description": "Invalid rule."}},
    tags=["Notification Preferences"],
)
async def create_preference(
    payload: NotificationPreferenceCreate, user: CurrentUser, session: DbSession
) -> NotificationPreferenceResponse:
    return await NotificationPreferenceService(session).create(user.id, payload)


@router.get(
    "/notification-preferences/{preference_id}",
    response_model=NotificationPreferenceResponse,
    summary="Get one notification rule",
    responses={**_AUTH_RESPONSES, **_NOT_FOUND},
    tags=["Notification Preferences"],
)
async def get_preference(
    preference_id: Annotated[uuid.UUID, Path(description="Preference identifier.")],
    user: CurrentUser,
    session: DbSession,
) -> NotificationPreferenceResponse:
    return await NotificationPreferenceService(session).get(preference_id, user.id)


@router.put(
    "/notification-preferences/{preference_id}",
    response_model=NotificationPreferenceResponse,
    summary="Update a notification rule",
    description=(
        "Updates a rule. Only the fields present in the request body are "
        "changed; everything else keeps its current value."
    ),
    responses={**_AUTH_RESPONSES, **_NOT_FOUND},
    tags=["Notification Preferences"],
)
async def update_preference(
    preference_id: Annotated[uuid.UUID, Path(description="Preference identifier.")],
    payload: NotificationPreferenceUpdate,
    user: CurrentUser,
    session: DbSession,
) -> NotificationPreferenceResponse:
    return await NotificationPreferenceService(session).update(preference_id, user.id, payload)


@router.delete(
    "/notification-preferences/{preference_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    # Explicit: a `-> None` return annotation would otherwise be inferred
    # as a response model, which FastAPI forbids on a 204.
    response_model=None,
    summary="Delete a notification rule",
    description="Permanently removes a rule. Past deliveries are removed with it.",
    responses={**_AUTH_RESPONSES, **_NOT_FOUND},
    tags=["Notification Preferences"],
)
async def delete_preference(
    preference_id: Annotated[uuid.UUID, Path(description="Preference identifier.")],
    user: CurrentUser,
    session: DbSession,
) -> None:
    await NotificationPreferenceService(session).delete(preference_id, user.id)


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------
@router.post(
    "/devices",
    response_model=DeviceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a device for push notifications",
    description=(
        "Registers a push target (FCM token for Android/iOS/Flutter, or a "
        "Web Push subscription for browsers).\n\n"
        "Registering an existing token is idempotent: the token is reassigned "
        "to the calling user and reactivated, which is what provider token "
        "rotation requires. Push tokens are never returned or logged."
    ),
    responses=_AUTH_RESPONSES,
    tags=["Devices"],
)
async def register_device(
    payload: DeviceRegisterRequest, user: CurrentUser, session: DbSession
) -> DeviceResponse:
    return await DeviceService(session).register(user.id, payload)


@router.get(
    "/devices",
    response_model=list[DeviceResponse],
    summary="List your registered devices",
    responses=_AUTH_RESPONSES,
    tags=["Devices"],
)
async def list_devices(user: CurrentUser, session: DbSession) -> list[DeviceResponse]:
    return await DeviceService(session).list_for_user(user.id)


@router.delete(
    "/devices/{device_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    # Explicit: a `-> None` return annotation would otherwise be inferred
    # as a response model, which FastAPI forbids on a 204.
    response_model=None,
    summary="Deregister a device",
    description="Deactivates a device so it stops receiving notifications.",
    responses={
        **_AUTH_RESPONSES,
        404: {"model": ErrorResponse, "description": "Device not found."},
    },
    tags=["Devices"],
)
async def deregister_device(
    device_id: Annotated[uuid.UUID, Path(description="Device identifier.")],
    user: CurrentUser,
    session: DbSession,
) -> None:
    await DeviceService(session).deregister(device_id, user.id)


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
@router.get(
    "/notifications",
    response_model=Page[NotificationDeliveryResponse],
    summary="List notifications sent to you",
    description=(
        "Paginated delivery history, newest first, including the GMP "
        "percentage that triggered each alert and its delivery status."
    ),
    responses=_AUTH_RESPONSES,
    tags=["Notification Preferences"],
)
async def list_notifications(
    user: CurrentUser, session: DbSession, pagination: Pagination
) -> Page[NotificationDeliveryResponse]:
    page, page_size = pagination
    return await NotificationHistoryService(session).list_for_user(
        user.id, page=page, page_size=page_size
    )
