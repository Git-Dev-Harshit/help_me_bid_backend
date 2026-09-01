"""FastAPI application factory and entrypoint.

The API process serves HTTP only.  Scraping and notification evaluation run in
a separate worker container (``app.workers.scheduler``) so a long scrape can
never delay a request, and so the two can be scaled independently.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import (
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.db.session import SessionFactory, dispose_engine
from app.schemas.common import HealthResponse

logger = get_logger(__name__)

VERSION = "1.0.0"

API_DESCRIPTION = """
Backend for tracking Indian IPOs, monitoring grey-market premium (GMP) and
delivering configurable IPO reminders.

### Authentication
Register with a phone number and password, then call `POST /api/v1/auth/login`
to obtain a JWT. Send it on protected endpoints as:

```
Authorization: Bearer <access_token>
```

### Notification rule
An alert is delivered only when the IPO **closes today** (in `APP_TIMEZONE`),
its **GMP percentage meets your threshold**, and your **interval has elapsed**.
Deduplication is enforced in the database, so no duplicate can be sent.

### Errors
Every failure returns the same envelope:

```json
{"success": false, "error": {"code": "IPO_NOT_FOUND", "message": "IPO not found"}}
```

Switch on the stable `error.code`; treat `error.message` as human-facing text.
"""

TAGS_METADATA = [
    {"name": "Authentication", "description": "Registration and login."},
    {"name": "Users", "description": "The authenticated user's profile."},
    {
        "name": "IPOs",
        "description": "Browse IPOs with filtering, search, sorting and pagination.",
    },
    {
        "name": "Notification Preferences",
        "description": "Configure when and how you are alerted, and review what was sent.",
    },
    {"name": "Devices", "description": "Register push targets for mobile and web clients."},
    {"name": "Health", "description": "Liveness and readiness probes."},
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start-up and shut-down hooks."""
    configure_logging()
    logger.info(
        "app.starting",
        extra={
            "version": VERSION,
            "environment": settings.app_env,
            "timezone": settings.app_timezone,
            "docs_enabled": settings.expose_docs,
        },
    )

    try:
        async with SessionFactory() as session:
            await session.execute(text("SELECT 1"))
        logger.info("app.database_connected")
    except Exception:
        # Do not abort start-up: the container should come up and report
        # unhealthy so the orchestrator can retry, rather than crash-looping.
        logger.error("app.database_unavailable", exc_info=True)

    yield

    await dispose_engine()
    logger.info("app.stopped")


def create_app() -> FastAPI:
    """Build and configure the application."""
    app = FastAPI(
        title=settings.app_name,
        description=API_DESCRIPTION,
        version=VERSION,
        lifespan=lifespan,
        docs_url=settings.docs_url,
        redoc_url=settings.redoc_url,
        openapi_url=settings.openapi_url,
        openapi_tags=TAGS_METADATA,
        contact={"name": "IPO Tracker API"},
        license_info={"name": "MIT"},
    )

    register_exception_handlers(app)

    # Middleware runs in reverse registration order, so the request-context
    # logger is added last to wrap everything below it.
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=settings.cors_allow_credentials,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
            expose_headers=["X-Request-ID"],
            max_age=600,
        )

    app.include_router(api_router, prefix=settings.api_v1_prefix)

    @app.get(
        "/health",
        response_model=HealthResponse,
        tags=["Health"],
        summary="Service health",
        description="Reports process health and database connectivity.",
    )
    async def health() -> HealthResponse:
        database = "ok"
        try:
            async with SessionFactory() as session:
                await session.execute(text("SELECT 1"))
        except Exception:
            logger.warning("health.database_unavailable", exc_info=True)
            database = "unavailable"
        return HealthResponse(
            status="ok" if database == "ok" else "degraded",
            version=VERSION,
            environment=settings.app_env,
            database=database,  # type: ignore[arg-type]
        )

    @app.get(
        "/health/live",
        tags=["Health"],
        summary="Liveness probe",
        description="Returns 200 whenever the process is running. No dependencies checked.",
    )
    async def liveness() -> dict[str, str]:
        return {"status": "alive"}

    return app


app = create_app()
