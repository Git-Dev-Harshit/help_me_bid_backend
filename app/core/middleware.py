"""HTTP middleware: request context logging, security headers, rate limiting."""

from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import settings
from app.core.errors import error_response
from app.core.logging import get_logger

logger = get_logger(__name__)

RequestHandler = Callable[[Request], Awaitable[Response]]

#: Endpoints protected by the rate limiter. Unauthenticated and credential
#: bearing, so they are the ones worth throttling.
RATE_LIMITED_PATHS = ("/auth/login", "/auth/register")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach a request id, time the request and log its completion."""

    async def dispatch(self, request: Request, call_next: RequestHandler) -> Response:
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        request.state.request_id = request_id
        started = time.perf_counter()

        response = await call_next(request)

        duration_ms = int((time.perf_counter() - started) * 1000)
        response.headers["X-Request-ID"] = request_id
        # Health checks fire constantly and would drown out real traffic.
        if request.url.path not in {"/health", "/health/live", "/health/ready"}:
            logger.info(
                "request.completed",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": duration_ms,
                },
            )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Apply conservative security headers.

    This service returns JSON only, so the CSP simply forbids everything - it
    costs nothing and neutralises any content sniffed as HTML.
    """

    async def dispatch(self, request: Request, call_next: RequestHandler) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Cache-Control", "no-store, no-cache, must-revalidate"
        )
        # The docs pages need to load their own JS/CSS from a CDN.
        if not request.url.path.startswith(("/docs", "/redoc")):
            response.headers.setdefault("Content-Security-Policy", "default-src 'none'")
        if settings.is_production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window rate limiting for credential endpoints.

    Deliberately in-process: it needs no Redis, adds no infrastructure and
    covers the realistic threat (one client hammering login).  The trade-off is
    that the limit applies *per API container*, so N replicas allow N times the
    configured rate.  When a global limit becomes necessary, enforce it at the
    load balancer or ingress rather than adding a shared store here.
    """

    def __init__(self, app: object, limit_per_minute: int | None = None) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._configured_limit = limit_per_minute
        self.window_seconds = 60
        self._hits: defaultdict[str, deque[float]] = defaultdict(deque)

    @property
    def limit(self) -> int:
        """Resolved per request, so the configured value is never stale."""
        return self._configured_limit or settings.rate_limit_auth_per_minute

    async def dispatch(self, request: Request, call_next: RequestHandler) -> Response:
        if not settings.rate_limit_enabled or not request.url.path.endswith(
            RATE_LIMITED_PATHS
        ):
            return await call_next(request)

        key = f"{self._client_ip(request)}:{request.url.path}"
        now = time.monotonic()
        hits = self._hits[key]

        while hits and now - hits[0] > self.window_seconds:
            hits.popleft()

        if len(hits) >= self.limit:
            retry_after = int(self.window_seconds - (now - hits[0])) + 1
            logger.warning(
                "request.rate_limited",
                extra={"path": request.url.path, "limit": self.limit},
            )
            return error_response(
                429,
                "RATE_LIMITED",
                "Too many requests. Please try again shortly.",
                headers={"Retry-After": str(retry_after)},
            )

        hits.append(now)
        self._prune(now)
        return await call_next(request)

    def _prune(self, now: float) -> None:
        """Drop idle keys so the dictionary cannot grow without bound."""
        if len(self._hits) < 1000:
            return
        for key in [k for k, v in self._hits.items() if not v or now - v[-1] > self.window_seconds]:
            del self._hits[key]

    @staticmethod
    def _client_ip(request: Request) -> str:
        """Best-effort client address.

        ``X-Forwarded-For`` is only meaningful behind a trusted proxy that
        overwrites it; treat it as a hint, never as identity.
        """
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"
