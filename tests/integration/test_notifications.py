"""Integration tests for the notification pipeline.

Covers the two guarantees that matter most: notifications fire only for IPOs
closing today above the user's GMP threshold, and the same notification can
never be delivered twice for one interval.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.core.security import hash_password
from app.db.models.device import Device
from app.db.models.notification import NotificationPreference
from app.db.models.user import User
from app.repositories.ipo import IPORepository
from app.services.notifications.engine import NotificationEngine
from app.services.notifications.providers import LogProvider, SendResult
from app.services.scraper.models import NormalizedIPO
from app.utils.dates import today_in_app_timezone

pytestmark = pytest.mark.integration

# Business date in APP_TIMEZONE - see the note in test_ipo_api.py.
TODAY = today_in_app_timezone()


class RecordingProvider(LogProvider):
    """Counts dispatches so tests can assert on real delivery attempts."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, message, targets) -> SendResult:
        self.sent.append(message.title)
        return SendResult(success=True, provider="log", provider_message_id="test")


async def make_user(session, phone="+919876543210") -> User:
    user = User(phone_number=phone, hashed_password=hash_password("Str0ngPass1"))
    session.add(user)
    await session.flush()
    session.add(
        Device(
            user_id=user.id,
            device_type="ANDROID",
            push_token=f"token-{phone}",
            is_active=True,
        )
    )
    await session.commit()
    return user


async def make_rule(session, user, **overrides) -> NotificationPreference:
    values = {
        "min_gmp_percentage": Decimal("15"),
        "interval_minutes": 180,
        "only_on_close_date": True,
        "channels": ["PUSH"],
        "is_enabled": True,
    }
    values.update(overrides)
    rule = NotificationPreference(user_id=user.id, **values)
    session.add(rule)
    await session.commit()
    return rule


async def make_ipo(session, **overrides):
    values: dict = {
        "source_ipo_id": "5001",
        "name": "Closing Today High GMP",
        "ipo_type": "MAINBOARD",
        "exchange": "NSE_BSE",
        "open_date": TODAY - dt.timedelta(days=2),
        "close_date": TODAY,
        "gmp": Decimal("44"),
        "gmp_percentage": Decimal("24.86"),
        "price_max": Decimal("177"),
    }
    values.update(overrides)
    await IPORepository(session).upsert_many([NormalizedIPO(**values)])
    await session.commit()


@pytest.fixture
def open_window(monkeypatch):
    """Force the quiet-hours check open so tests are time-independent."""
    monkeypatch.setattr(
        "app.services.notifications.engine.within_notification_window", lambda *_: True
    )


class TestEligibility:
    async def test_sends_for_an_ipo_closing_today_above_threshold(
        self, db_session, open_window
    ):
        user = await make_user(db_session)
        await make_rule(db_session, user)
        await make_ipo(db_session)

        provider = RecordingProvider()
        summary = await NotificationEngine(db_session, provider=provider).evaluate()

        assert summary.matches == 1
        assert summary.sent == 1
        assert provider.sent == ["Closing Today High GMP closes today"]

    @pytest.mark.parametrize("offset", [-1, 1, 5])
    async def test_never_sends_when_the_ipo_is_not_closing_today(
        self, db_session, open_window, offset
    ):
        user = await make_user(db_session)
        await make_rule(db_session, user)
        await make_ipo(db_session, close_date=TODAY + dt.timedelta(days=offset))

        provider = RecordingProvider()
        summary = await NotificationEngine(db_session, provider=provider).evaluate()

        assert summary.matches == 0
        assert summary.sent == 0
        assert provider.sent == []

    async def test_does_not_send_below_the_gmp_threshold(self, db_session, open_window):
        user = await make_user(db_session)
        await make_rule(db_session, user, min_gmp_percentage=Decimal("30"))
        await make_ipo(db_session, gmp_percentage=Decimal("24.86"))

        summary = await NotificationEngine(db_session, provider=RecordingProvider()).evaluate()
        assert summary.sent == 0

    async def test_disabled_rules_are_skipped(self, db_session, open_window):
        user = await make_user(db_session)
        await make_rule(db_session, user, is_enabled=False)
        await make_ipo(db_session)

        summary = await NotificationEngine(db_session, provider=RecordingProvider()).evaluate()
        assert summary.rules_evaluated == 0
        assert summary.sent == 0

    async def test_inactive_users_are_skipped(self, db_session, open_window):
        user = await make_user(db_session)
        await make_rule(db_session, user)
        await make_ipo(db_session)
        user.is_active = False
        await db_session.commit()

        summary = await NotificationEngine(db_session, provider=RecordingProvider()).evaluate()
        assert summary.rules_evaluated == 0

    async def test_quiet_hours_block_delivery(self, db_session, monkeypatch):
        monkeypatch.setattr(
            "app.services.notifications.engine.within_notification_window", lambda *_: False
        )
        user = await make_user(db_session)
        await make_rule(db_session, user)
        await make_ipo(db_session)

        summary = await NotificationEngine(db_session, provider=RecordingProvider()).evaluate()
        assert summary.sent == 0

    async def test_ipo_type_filter_is_applied(self, db_session, open_window):
        user = await make_user(db_session)
        await make_rule(db_session, user, ipo_types=["SME"])
        await make_ipo(db_session, ipo_type="MAINBOARD")

        summary = await NotificationEngine(db_session, provider=RecordingProvider()).evaluate()
        assert summary.matches == 0

    async def test_skipped_when_the_user_has_no_device(self, db_session, open_window):
        user = User(phone_number="+919000000001", hashed_password=hash_password("Str0ngPass1"))
        db_session.add(user)
        await db_session.flush()
        await make_rule(db_session, user)
        await make_ipo(db_session)

        summary = await NotificationEngine(db_session, provider=RecordingProvider()).evaluate()
        assert summary.claimed == 1
        assert summary.skipped_no_device == 1
        assert summary.sent == 0


class TestDeduplication:
    async def test_repeated_runs_send_only_once(self, db_session, open_window):
        """The scheduler fires every 15 minutes; the rule interval is 3 hours."""
        user = await make_user(db_session)
        await make_rule(db_session, user, interval_minutes=180)
        await make_ipo(db_session)

        provider = RecordingProvider()
        engine = NotificationEngine(db_session, provider=provider)

        first = await engine.evaluate()
        second = await engine.evaluate()
        third = await engine.evaluate()

        assert first.sent == 1
        assert (second.sent, second.duplicates_skipped) == (0, 1)
        assert (third.sent, third.duplicates_skipped) == (0, 1)
        assert len(provider.sent) == 1

    async def test_only_one_delivery_row_exists(self, db_session, open_window):
        user = await make_user(db_session)
        await make_rule(db_session, user)
        await make_ipo(db_session)

        engine = NotificationEngine(db_session, provider=RecordingProvider())
        await engine.evaluate()
        await engine.evaluate()

        from sqlalchemy import func, select

        from app.db.models.notification import NotificationDelivery

        total = (
            await db_session.execute(select(func.count()).select_from(NotificationDelivery))
        ).scalar_one()
        assert total == 1

    async def test_concurrent_claims_yield_exactly_one_winner(self, db_session, open_window):
        """The unique constraint - not a lock - is what prevents duplicates."""

        from app.repositories.notification import NotificationDeliveryRepository

        user = await make_user(db_session)
        rule = await make_rule(db_session, user)
        await make_ipo(db_session)
        rows, _ = await IPORepository(db_session).list_filtered(_all_filters())
        ipo_id = rows[0].id

        repository = NotificationDeliveryRepository(db_session)
        claims = []
        for _ in range(5):
            claim = await repository.claim(
                user_id=user.id,
                preference_id=rule.id,
                ipo_id=ipo_id,
                channel="PUSH",
                period_key=999,
                business_date=TODAY,
                gmp_percentage=Decimal("24.86"),
                payload={},
            )
            claims.append(claim)
            await db_session.commit()

        assert sum(1 for c in claims if c is not None) == 1

    async def test_a_new_interval_allows_another_send(self, db_session, open_window):

        from app.repositories.notification import NotificationDeliveryRepository

        user = await make_user(db_session)
        rule = await make_rule(db_session, user)
        await make_ipo(db_session)
        rows, _ = await IPORepository(db_session).list_filtered(_all_filters())
        ipo_id = rows[0].id

        repository = NotificationDeliveryRepository(db_session)
        first = await repository.claim(
            user_id=user.id, preference_id=rule.id, ipo_id=ipo_id, channel="PUSH",
            period_key=1000, business_date=TODAY, gmp_percentage=None, payload={},
        )
        await db_session.commit()
        second = await repository.claim(
            user_id=user.id, preference_id=rule.id, ipo_id=ipo_id, channel="PUSH",
            period_key=1001, business_date=TODAY, gmp_percentage=None, payload={},
        )
        await db_session.commit()

        assert first is not None and second is not None


class TestPreferenceApi:
    async def test_full_crud_cycle(self, api_client):
        await api_client.post(
            "/api/v1/auth/register",
            json={"phone_number": "9876543210", "password": "Str0ngPass1"},
        )
        token = (
            await api_client.post(
                "/api/v1/auth/login",
                json={"phone_number": "9876543210", "password": "Str0ngPass1"},
            )
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        created = await api_client.post(
            "/api/v1/notification-preferences",
            json={"min_gmp_percentage": 15, "interval_minutes": 180, "only_on_close_date": True},
            headers=headers,
        )
        assert created.status_code == 201
        preference_id = created.json()["id"]

        listed = await api_client.get("/api/v1/notification-preferences", headers=headers)
        assert len(listed.json()) == 1

        updated = await api_client.put(
            f"/api/v1/notification-preferences/{preference_id}",
            json={"min_gmp_percentage": 25},
            headers=headers,
        )
        assert updated.status_code == 200
        assert Decimal(updated.json()["min_gmp_percentage"]) == Decimal("25")
        assert updated.json()["interval_minutes"] == 180  # untouched

        deleted = await api_client.delete(
            f"/api/v1/notification-preferences/{preference_id}", headers=headers
        )
        assert deleted.status_code == 204
        assert (
            await api_client.get("/api/v1/notification-preferences", headers=headers)
        ).json() == []

    async def test_one_user_cannot_reach_anothers_rule(self, api_client):
        """Returns 404, not 403, so ids cannot be probed for existence."""
        async def account(phone):
            await api_client.post(
                "/api/v1/auth/register",
                json={"phone_number": phone, "password": "Str0ngPass1"},
            )
            token = (
                await api_client.post(
                    "/api/v1/auth/login",
                    json={"phone_number": phone, "password": "Str0ngPass1"},
                )
            ).json()["access_token"]
            return {"Authorization": f"Bearer {token}"}

        owner = await account("9876543210")
        intruder = await account("9876543211")

        preference_id = (
            await api_client.post(
                "/api/v1/notification-preferences",
                json={"min_gmp_percentage": 15, "interval_minutes": 180},
                headers=owner,
            )
        ).json()["id"]

        response = await api_client.get(
            f"/api/v1/notification-preferences/{preference_id}", headers=intruder
        )
        assert response.status_code == 404


def _all_filters():
    from app.schemas.ipo import IPOFilterParams

    return IPOFilterParams(page_size=100)
