"""Application exception hierarchy.

Every error the API deliberately returns derives from :class:`AppError`, which
carries a stable machine-readable ``code`` alongside the HTTP status.  Clients
switch on ``error.code``; the human ``message`` is free to change.

Anything *not* deriving from ``AppError`` is treated as an unexpected failure:
logged with a traceback internally, reported to the client as a generic
``INTERNAL_ERROR``.  Database and stack details never reach a response body.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for all deliberate, client-visible application errors."""

    status_code: int = 500
    code: str = "INTERNAL_ERROR"
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.status_code = status_code or self.status_code
        self.details = details
        super().__init__(self.message)


# --- 4xx -------------------------------------------------------------------
class BadRequestError(AppError):
    status_code = 400
    code = "BAD_REQUEST"
    message = "The request could not be processed."


class ValidationError(AppError):
    status_code = 422
    code = "VALIDATION_ERROR"
    message = "The request payload failed validation."


class AuthenticationError(AppError):
    status_code = 401
    code = "AUTHENTICATION_FAILED"
    message = "Authentication failed."


class InvalidCredentialsError(AuthenticationError):
    code = "INVALID_CREDENTIALS"
    # Deliberately identical whether the phone number exists or the password is
    # wrong, so the endpoint cannot be used to enumerate registered accounts.
    message = "Incorrect phone number or password."


class InvalidTokenError(AuthenticationError):
    code = "INVALID_TOKEN"
    message = "The access token is invalid or has expired."


class InactiveUserError(AuthenticationError):
    status_code = 403
    code = "USER_INACTIVE"
    message = "This account has been deactivated."


class PermissionDeniedError(AppError):
    status_code = 403
    code = "PERMISSION_DENIED"
    message = "You do not have permission to perform this action."


class NotFoundError(AppError):
    status_code = 404
    code = "NOT_FOUND"
    message = "The requested resource was not found."


class IPONotFoundError(NotFoundError):
    code = "IPO_NOT_FOUND"
    message = "IPO not found."


class UserNotFoundError(NotFoundError):
    code = "USER_NOT_FOUND"
    message = "User not found."


class NotificationPreferenceNotFoundError(NotFoundError):
    code = "NOTIFICATION_PREFERENCE_NOT_FOUND"
    message = "Notification preference not found."


class DeviceNotFoundError(NotFoundError):
    code = "DEVICE_NOT_FOUND"
    message = "Device not found."


class ConflictError(AppError):
    status_code = 409
    code = "CONFLICT"
    message = "The resource is in a conflicting state."


class PhoneAlreadyRegisteredError(ConflictError):
    code = "PHONE_ALREADY_REGISTERED"
    message = "An account with this phone number already exists."


class RateLimitedError(AppError):
    status_code = 429
    code = "RATE_LIMITED"
    message = "Too many requests. Please try again shortly."


# --- Scraper ---------------------------------------------------------------
class ScraperError(AppError):
    """Base class for scraping failures (internal; not surfaced to clients)."""

    status_code = 502
    code = "SCRAPER_ERROR"
    message = "Failed to retrieve IPO data from the upstream source."


class FetchError(ScraperError):
    code = "SCRAPER_FETCH_FAILED"
    message = "Could not fetch the upstream IPO source."


class ExtractionError(ScraperError):
    code = "SCRAPER_EXTRACTION_FAILED"
    message = "Could not locate an IPO dataset in the upstream response."


class LowConfidenceError(ScraperError):
    code = "SCRAPER_LOW_CONFIDENCE"
    message = "Extraction confidence fell below the configured threshold."


# --- Notifications ---------------------------------------------------------
class NotificationError(AppError):
    status_code = 502
    code = "NOTIFICATION_ERROR"
    message = "Failed to deliver the notification."


class ProviderNotConfiguredError(NotificationError):
    code = "NOTIFICATION_PROVIDER_NOT_CONFIGURED"
    message = "The notification provider is not configured."
