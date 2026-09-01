"""HTTP fetching for the scraper.

One ``httpx.AsyncClient`` is reused for the process, so TCP and TLS handshakes
are amortised across scrapes rather than repeated every 30 minutes.

The upstream site rejects requests without browser-like headers (it answers
``403`` to a bare client), so a realistic ``User-Agent`` and ``Referer`` are
sent.  Retries use exponential backoff and only cover transient conditions -
timeouts, connection errors, ``5xx`` and ``429``.  A ``404`` is not retried,
because retrying it cannot help.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib

import httpx

from app.core.config import settings
from app.core.exceptions import FetchError
from app.core.logging import get_logger
from app.services.scraper.models import RawPayload

logger = get_logger(__name__)

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()


class ScraperHTTPClient:
    """Async HTTP client with retry/backoff, shared connection pooling."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> ScraperHTTPClient:
        if self._client is None:
            self._client = self._build_client()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _build_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(settings.scraper_timeout_seconds),
            follow_redirects=True,
            http2=True,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            headers={
                "User-Agent": settings.scraper_user_agent,
                "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                # Requests without a matching Referer/Origin are refused.
                "Referer": settings.scraper_page_url,
                "Origin": "https://www.investorgain.com",
                "Cache-Control": "no-cache",
            },
        )

    async def fetch(self, url: str) -> RawPayload:
        """GET ``url``, retrying transient failures, and return the raw body."""
        if self._client is None:
            self._client = self._build_client()
            self._owns_client = True

        last_error: Exception | None = None
        attempts = settings.scraper_max_retries + 1

        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.get(url)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                logger.warning(
                    "scraper.fetch_attempt_failed",
                    extra={"url": url, "attempt": attempt, "reason": type(exc).__name__},
                )
            else:
                if response.status_code in RETRYABLE_STATUS and attempt < attempts:
                    last_error = httpx.HTTPStatusError(
                        f"HTTP {response.status_code}", request=response.request,
                        response=response,
                    )
                    logger.warning(
                        "scraper.fetch_attempt_failed",
                        extra={
                            "url": url,
                            "attempt": attempt,
                            "http_status": response.status_code,
                        },
                    )
                elif response.is_success:
                    content = response.text
                    logger.info(
                        "scraper.fetch_succeeded",
                        extra={
                            "url": url,
                            "http_status": response.status_code,
                            "bytes": len(content),
                            "attempt": attempt,
                        },
                    )
                    return RawPayload(
                        url=url,
                        content=content,
                        content_type=response.headers.get("content-type", "unknown"),
                        http_status=response.status_code,
                        fetched_at=dt.datetime.now(dt.UTC),
                        content_hash=_hash(content),
                    )
                else:
                    raise FetchError(
                        f"Upstream returned HTTP {response.status_code} for {url}",
                        details={"http_status": response.status_code, "url": url},
                    )

            if attempt < attempts:
                await asyncio.sleep(settings.scraper_retry_backoff_seconds * (2 ** (attempt - 1)))

        raise FetchError(
            f"Failed to fetch {url} after {attempts} attempts",
            details={"url": url, "cause": type(last_error).__name__ if last_error else None},
        ) from last_error


def build_report_api_url(
    report_id: int | None = None,
    page: int = 1,
    today: dt.date | None = None,
) -> str:
    """Build the upstream JSON report URL.

    Path shape (discovered from the page's own client bundle)::

        /cloud/v2/report/data-read/{report}/{page}/{month}/{year}/{fy}/{sort}/{param}

    ``{fy}`` is the Indian financial year (April-March) and ``{param}`` must be
    ``all`` - any other value makes the endpoint answer ``msg: -1`` with no rows.
    """
    report_id = report_id or settings.scraper_report_id
    today = today or dt.datetime.now(settings.timezone).date()
    financial_year = (
        f"{today.year}-{str(today.year + 1)[2:]}"
        if today.month >= 4
        else f"{today.year - 1}-{str(today.year)[2:]}"
    )
    base = settings.scraper_api_base_url.rstrip("/")
    return (
        f"{base}/cloud/v2/report/data-read/{report_id}/{page}/"
        f"{today.month}/{today.year}/{financial_year}/0/all?search=&v=1"
    )
