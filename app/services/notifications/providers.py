"""Notification transport providers.

Every provider implements one small interface, so the delivery engine never
knows which transport it is using::

    NotificationService
            |
            +-- LogProvider       (default; no credentials needed)
            +-- FCMProvider       (Android / iOS / Flutter, via FCM HTTP v1)
            +-- WebPushProvider   (browsers, via VAPID)

``LogProvider`` is the default so the whole notification pipeline - rule
evaluation, deduplication, the delivery ledger - runs and can be exercised
end-to-end before any push credentials exist.  Swapping in a real transport is
an environment-variable change, not a code change.

FCM and Web Push need third-party client libraries that the base image does not
carry.  They are imported lazily inside ``send`` so the dependency is only
required when that provider is actually selected; install them with the
matching optional extra (see ``pyproject.toml``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.core.exceptions import ProviderNotConfiguredError
from app.core.logging import get_logger
from app.db.enums import DeviceType, NotificationChannel

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class NotificationMessage:
    """Transport-agnostic notification content."""

    title: str
    body: str
    #: Structured payload for the client (deep links, ids). Values must be
    #: strings: FCM rejects non-string data values.
    data: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DeviceTarget:
    """One push destination."""

    device_id: str
    device_type: DeviceType
    push_token: str


@dataclass(slots=True)
class SendResult:
    """Outcome of one dispatch attempt."""

    success: bool
    provider: str
    provider_message_id: str | None = None
    error: str | None = None
    #: Tokens the provider reported as permanently invalid; the caller
    #: deactivates them so they are not retried forever.
    invalid_tokens: list[str] = field(default_factory=list)


class NotificationProvider(ABC):
    """Interface every transport implements."""

    name: str
    channel: NotificationChannel

    @property
    @abstractmethod
    def is_configured(self) -> bool:
        """True when this provider has everything it needs to send."""

    @abstractmethod
    async def send(
        self, message: NotificationMessage, targets: list[DeviceTarget]
    ) -> SendResult:
        """Deliver ``message`` to every target."""


class LogProvider(NotificationProvider):
    """Default sink: records the notification instead of transmitting it.

    Lets the full pipeline be validated (and integration-tested) without
    external credentials.  Push tokens are never logged.
    """

    name = "log"
    channel = NotificationChannel.LOG

    @property
    def is_configured(self) -> bool:
        return True

    async def send(
        self, message: NotificationMessage, targets: list[DeviceTarget]
    ) -> SendResult:
        logger.info(
            "notification.dispatched",
            extra={
                "provider": self.name,
                "title": message.title,
                "target_count": len(targets),
                "device_types": sorted({t.device_type.value for t in targets}),
                "ipo_id": message.data.get("ipo_id"),
            },
        )
        return SendResult(success=True, provider=self.name, provider_message_id="logged")


class FCMProvider(NotificationProvider):
    """Firebase Cloud Messaging (HTTP v1) - Android, iOS and Flutter clients.

    Requires ``FCM_CREDENTIALS_FILE`` (a service-account JSON) and
    ``FCM_PROJECT_ID``.  Install the extra first::

        pip install "ipo-tracker[fcm]"
    """

    name = "fcm"
    channel = NotificationChannel.PUSH
    _SCOPE = "https://www.googleapis.com/auth/firebase.messaging"

    @property
    def is_configured(self) -> bool:
        return bool(settings.fcm_credentials_file and settings.fcm_project_id)

    async def send(
        self, message: NotificationMessage, targets: list[DeviceTarget]
    ) -> SendResult:
        if not self.is_configured:
            raise ProviderNotConfiguredError(
                "FCM requires FCM_CREDENTIALS_FILE and FCM_PROJECT_ID to be set."
            )
        try:
            import google.auth.transport.requests
            from google.oauth2 import service_account
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise ProviderNotConfiguredError(
                "The FCM provider needs the 'fcm' extra: pip install 'ipo-tracker[fcm]'"
            ) from exc

        import asyncio

        import httpx

        credentials = service_account.Credentials.from_service_account_file(
            settings.fcm_credentials_file, scopes=[self._SCOPE]
        )
        # google-auth is synchronous; refresh off the event loop.
        await asyncio.to_thread(
            credentials.refresh, google.auth.transport.requests.Request()
        )

        endpoint = (
            f"https://fcm.googleapis.com/v1/projects/{settings.fcm_project_id}/messages:send"
        )
        headers = {
            "Authorization": f"Bearer {credentials.token}",
            "Content-Type": "application/json",
        }

        sent = 0
        invalid: list[str] = []
        last_error: str | None = None
        last_id: str | None = None

        async with httpx.AsyncClient(timeout=15.0) as client:
            for target in targets:
                payload: dict[str, Any] = {
                    "message": {
                        "token": target.push_token,
                        "notification": {"title": message.title, "body": message.body},
                        "data": message.data,
                    }
                }
                try:
                    response = await client.post(endpoint, headers=headers, json=payload)
                except httpx.HTTPError as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    continue

                if response.is_success:
                    sent += 1
                    last_id = response.json().get("name")
                elif response.status_code in (400, 403, 404):
                    # UNREGISTERED / INVALID_ARGUMENT: the token is dead.
                    invalid.append(target.push_token)
                    last_error = f"HTTP {response.status_code}"
                else:
                    last_error = f"HTTP {response.status_code}"

        return SendResult(
            success=sent > 0,
            provider=self.name,
            provider_message_id=last_id,
            error=None if sent else (last_error or "no targets accepted the message"),
            invalid_tokens=invalid,
        )


class WebPushProvider(NotificationProvider):
    """Browser Web Push over VAPID.

    Requires ``VAPID_PUBLIC_KEY``, ``VAPID_PRIVATE_KEY`` and ``VAPID_SUBJECT``.
    Install the extra first::

        pip install "ipo-tracker[webpush]"
    """

    name = "webpush"
    channel = NotificationChannel.WEBPUSH

    @property
    def is_configured(self) -> bool:
        return bool(settings.vapid_private_key and settings.vapid_public_key)

    async def send(
        self, message: NotificationMessage, targets: list[DeviceTarget]
    ) -> SendResult:
        if not self.is_configured:
            raise ProviderNotConfiguredError(
                "Web Push requires VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY to be set."
            )
        try:
            from pywebpush import WebPushException, webpush
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise ProviderNotConfiguredError(
                "The Web Push provider needs the 'webpush' extra: "
                "pip install 'ipo-tracker[webpush]'"
            ) from exc

        import asyncio
        import json

        body = json.dumps(
            {"title": message.title, "body": message.body, "data": message.data}
        )
        sent = 0
        invalid: list[str] = []
        last_error: str | None = None

        for target in targets:
            try:
                # The subscription info is stored as the JSON token issued by
                # the browser's PushManager.
                subscription = json.loads(target.push_token)
                await asyncio.to_thread(
                    webpush,
                    subscription_info=subscription,
                    data=body,
                    vapid_private_key=settings.vapid_private_key,
                    vapid_claims={"sub": settings.vapid_subject},
                )
                sent += 1
            except WebPushException as exc:  # pragma: no cover - network path
                status = getattr(exc.response, "status_code", None)
                if status in (404, 410):  # subscription gone
                    invalid.append(target.push_token)
                last_error = str(exc)
            except (ValueError, json.JSONDecodeError) as exc:
                invalid.append(target.push_token)
                last_error = f"malformed subscription: {exc}"

        return SendResult(
            success=sent > 0,
            provider=self.name,
            error=None if sent else (last_error or "no subscriptions accepted the message"),
            invalid_tokens=invalid,
        )


_PROVIDERS: dict[str, type[NotificationProvider]] = {
    LogProvider.name: LogProvider,
    FCMProvider.name: FCMProvider,
    WebPushProvider.name: WebPushProvider,
}


def get_provider(name: str | None = None) -> NotificationProvider:
    """Instantiate the configured provider.

    An unconfigured non-default provider falls back to :class:`LogProvider`
    rather than failing: notifications are still evaluated and recorded, and
    the gap is visible in the logs instead of taking the worker down.
    """
    name = name or settings.notification_provider
    provider_cls = _PROVIDERS.get(name, LogProvider)
    provider = provider_cls()
    if not provider.is_configured:
        logger.warning(
            "notification.provider_not_configured",
            extra={"requested_provider": name, "using": LogProvider.name},
        )
        return LogProvider()
    return provider
