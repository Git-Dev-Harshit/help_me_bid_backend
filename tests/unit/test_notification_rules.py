"""Notification rule evaluation tests (spec section 12).

These cover the headline business rule directly, with no database or provider:
close-date restriction, GMP threshold, optional narrowing filters, the quiet
hours window, and the interval bucketing that makes deduplication possible.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.notifications.engine import (
    build_message,
    rule_matches_ipo,
    within_notification_window,
)
from app.utils.dates import period_index

TODAY = dt.date(2026, 9, 1)


def make_rule(**overrides):
    values = {
        "id": "rule-1",
        "min_gmp_percentage": Decimal("15"),
        "max_gmp_percentage": None,
        "interval_minutes": 180,
        "only_on_close_date": True,
        "ipo_types": None,
        "exchanges": None,
        "min_subscription_times": None,
        "channels": ["PUSH"],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_ipo(**overrides):
    values = {
        "id": "ipo-1",
        "name": "Deepa Jewellers",
        "close_date": TODAY,
        "gmp_percentage": Decimal("24.86"),
        "gmp": Decimal("44"),
        "price_max": Decimal("177"),
        "ipo_type": "MAINBOARD",
        "exchange": "NSE_BSE",
        "subscription_times": Decimal("0.88"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class TestCloseDateRestriction:
    def test_matches_when_closing_today(self):
        assert rule_matches_ipo(make_rule(), make_ipo(), TODAY)

    @pytest.mark.parametrize("offset", [-1, 1, 7])
    def test_never_matches_when_not_closing_today(self, offset):
        """The core rule: no alert unless the IPO closes today."""
        ipo = make_ipo(close_date=TODAY + dt.timedelta(days=offset))
        assert not rule_matches_ipo(make_rule(), ipo, TODAY)

    def test_high_gmp_does_not_override_the_date_rule(self):
        ipo = make_ipo(close_date=TODAY + dt.timedelta(days=2), gmp_percentage=Decimal("500"))
        assert not rule_matches_ipo(make_rule(), ipo, TODAY)

    def test_missing_close_date_never_matches(self):
        assert not rule_matches_ipo(make_rule(), make_ipo(close_date=None), TODAY)

    def test_opting_out_allows_other_days(self):
        ipo = make_ipo(close_date=TODAY + dt.timedelta(days=3))
        assert rule_matches_ipo(make_rule(only_on_close_date=False), ipo, TODAY)


class TestGMPThreshold:
    @pytest.mark.parametrize(
        ("gmp_percentage", "expected"),
        [
            (Decimal("14.99"), False),
            (Decimal("15"), True),  # boundary is inclusive
            (Decimal("15.01"), True),
            (Decimal("0"), False),
            (Decimal("-5"), False),
        ],
    )
    def test_minimum_threshold(self, gmp_percentage, expected):
        ipo = make_ipo(gmp_percentage=gmp_percentage)
        assert rule_matches_ipo(make_rule(), ipo, TODAY) is expected

    def test_maximum_threshold(self):
        rule = make_rule(max_gmp_percentage=Decimal("30"))
        assert rule_matches_ipo(rule, make_ipo(gmp_percentage=Decimal("25")), TODAY)
        assert not rule_matches_ipo(rule, make_ipo(gmp_percentage=Decimal("35")), TODAY)

    def test_unknown_gmp_never_matches(self):
        """No GMP data means no basis for an alert."""
        assert not rule_matches_ipo(make_rule(), make_ipo(gmp_percentage=None), TODAY)


class TestNarrowingFilters:
    def test_ipo_type_filter(self):
        rule = make_rule(ipo_types=["SME"])
        assert not rule_matches_ipo(rule, make_ipo(ipo_type="MAINBOARD"), TODAY)
        assert rule_matches_ipo(rule, make_ipo(ipo_type="SME"), TODAY)

    def test_exchange_filter(self):
        rule = make_rule(exchanges=["NSE_SME", "BSE_SME"])
        assert not rule_matches_ipo(rule, make_ipo(exchange="NSE_BSE"), TODAY)
        assert rule_matches_ipo(rule, make_ipo(exchange="BSE_SME"), TODAY)

    def test_empty_filter_means_no_restriction(self):
        assert rule_matches_ipo(make_rule(ipo_types=[]), make_ipo(), TODAY)

    def test_minimum_subscription_filter(self):
        rule = make_rule(min_subscription_times=Decimal("2"))
        assert not rule_matches_ipo(rule, make_ipo(subscription_times=Decimal("0.88")), TODAY)
        assert rule_matches_ipo(rule, make_ipo(subscription_times=Decimal("10")), TODAY)


class TestNotificationWindow:
    @pytest.mark.parametrize(
        ("hour", "expected"), [(7, False), (8, True), (14, True), (21, True), (22, False)]
    )
    def test_quiet_hours(self, hour, expected, monkeypatch):
        from app.core.config import settings

        instant = dt.datetime(2026, 9, 1, hour, 30, tzinfo=settings.timezone)
        assert within_notification_window(instant) is expected


class TestIntervalBucketing:
    def test_same_window_yields_the_same_key(self):
        """Two runs inside one interval must collide, which is what dedupes."""
        base = dt.datetime(2026, 9, 1, 10, 0, tzinfo=dt.UTC)
        assert period_index(base, 180) == period_index(base + dt.timedelta(minutes=59), 180)

    def test_next_window_yields_a_new_key(self):
        base = dt.datetime(2026, 9, 1, 10, 0, tzinfo=dt.UTC)
        assert period_index(base, 180) != period_index(base + dt.timedelta(hours=3), 180)

    def test_key_is_independent_of_the_caller_timezone(self):
        import zoneinfo

        utc = dt.datetime(2026, 9, 1, 10, 0, tzinfo=dt.UTC)
        ist = utc.astimezone(zoneinfo.ZoneInfo("Asia/Kolkata"))
        assert period_index(utc, 180) == period_index(ist, 180)

    def test_rejects_a_nonsensical_interval(self):
        with pytest.raises(ValueError):
            period_index(dt.datetime.now(dt.UTC), 0)


class TestMessageBuilding:
    def test_message_carries_the_key_facts(self):
        message = build_message(make_ipo(), make_rule())
        assert "Deepa Jewellers" in message.title
        assert "closes today" in message.title
        assert "24.86" in message.body

    def test_data_values_are_all_strings(self):
        """FCM rejects non-string data payload values."""
        message = build_message(make_ipo(), make_rule())
        assert all(isinstance(value, str) for value in message.data.values())
        assert message.data["type"] == "ipo_gmp_alert"
