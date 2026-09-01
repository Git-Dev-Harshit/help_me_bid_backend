"""Rate limiting on the credential endpoints.

Isolated in its own module because the limiter keeps in-process state: the rest
of the suite runs with ``RATE_LIMIT_ENABLED=false`` so that test order cannot
affect results, and these tests switch it on deliberately.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def limiter_on(monkeypatch):
    """Enable the limiter with a small window for this test only."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_auth_per_minute", 3)
    yield


class TestAuthRateLimit:
    async def test_blocks_after_the_configured_number_of_attempts(
        self, api_client, limiter_on
    ):
        payload = {"phone_number": "9876543210", "password": "Wr0ngPass1"}

        statuses = []
        for _ in range(8):
            response = await api_client.post("/api/v1/auth/login", json=payload)
            statuses.append(response.status_code)

        assert 429 in statuses, f"limiter never engaged: {statuses}"
        limited = await api_client.post("/api/v1/auth/login", json=payload)
        assert limited.status_code == 429
        assert limited.json()["error"]["code"] == "RATE_LIMITED"
        assert "Retry-After" in limited.headers

    async def test_other_endpoints_are_not_limited(self, api_client, limiter_on):
        """Only credential endpoints are throttled; browsing IPOs is not."""
        for _ in range(10):
            response = await api_client.get("/api/v1/ipos?page_size=1")
            assert response.status_code == 200
