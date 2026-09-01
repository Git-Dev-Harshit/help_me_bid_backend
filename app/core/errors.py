"""Centralised exception handling.

Every failure leaves the API in the same envelope::

    {"success": false, "error": {"code": "...", "message": "...", "details": ...}}

Deliberate :class:`AppError` subclasses map to their own status and code.
Anything else is logged with a full traceback and reported as a generic
``INTERNAL_ERROR`` - stack traces, SQL and driver messages never reach a
client.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import AppError
from app.core.logging import get_logger

logger = get_logger(__name__)


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build the standard error envelope."""
    payload: dict[str, Any] = {"code": code, "message": message}
    if details:
        payload["details"] = details
    return JSONResponse(
        status_code=status_code,
        content={"success": False, "error": payload},
        headers=headers,
    )


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """Handle deliberate application errors."""
    log = logger.warning if exc.status_code < 500 else logger.error
    log(
        "request.failed",
        extra={
            "path": request.url.path,
            "method": request.method,
            "error_code": exc.code,
            "status_code": exc.status_code,
        },
    )
    headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
    return error_response(exc.status_code, exc.code, exc.message, exc.details, headers)


async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Convert FastAPI/Pydantic validation failures into the shared envelope."""
    details: dict[str, Any] = {}
    for error in exc.errors():
        # Drop the leading location kind ("body"/"query") for a cleaner key.
        location = ".".join(str(part) for part in error["loc"][1:]) or "request"
        details.setdefault(location, error["msg"])
    logger.info(
        "request.validation_failed",
        extra={"path": request.url.path, "fields": sorted(details)},
    )
    return error_response(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        "VALIDATION_ERROR",
        "The request payload failed validation.",
        details,
    )


async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Normalise framework-raised HTTP errors (404 routing, 405, ...)."""
    codes = {
        status.HTTP_401_UNAUTHORIZED: "AUTHENTICATION_FAILED",
        status.HTTP_403_FORBIDDEN: "PERMISSION_DENIED",
        status.HTTP_404_NOT_FOUND: "NOT_FOUND",
        status.HTTP_405_METHOD_NOT_ALLOWED: "METHOD_NOT_ALLOWED",
        status.HTTP_429_TOO_MANY_REQUESTS: "RATE_LIMITED",
    }
    code = codes.get(exc.status_code, "HTTP_ERROR")
    message = exc.detail if isinstance(exc.detail, str) else "Request failed."
    return error_response(
        exc.status_code, code, message, headers=getattr(exc, "headers", None)
    )


async def integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
    """Turn a constraint violation into a 409 without leaking the SQL."""
    logger.warning(
        "request.integrity_error",
        extra={"path": request.url.path, "method": request.method},
        exc_info=True,
    )
    return error_response(
        status.HTTP_409_CONFLICT,
        "CONFLICT",
        "The request conflicts with existing data.",
    )


async def database_error_handler(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    """Log database failures in full; tell the client nothing specific."""
    logger.error(
        "request.database_error",
        extra={"path": request.url.path, "method": request.method},
        exc_info=True,
    )
    return error_response(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "DATABASE_UNAVAILABLE",
        "A database error occurred. Please try again.",
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last resort: log the traceback, return an opaque 500."""
    logger.error(
        "request.unhandled_error",
        extra={"path": request.url.path, "method": request.method},
        exc_info=True,
    )
    return error_response(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "INTERNAL_ERROR",
        "An unexpected error occurred.",
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach every handler. Order is irrelevant; FastAPI matches by type."""
    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(IntegrityError, integrity_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(SQLAlchemyError, database_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_error_handler)
