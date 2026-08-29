"""Tests for operator alerts."""

import pytest

from bcn.common.alerts import quiet_streak_alert_due
from bcn.common.alerts import send_operator_alert
from bcn.common.config import Settings


def test_quiet_streak_alert_cadence():
    assert not quiet_streak_alert_due(3, 4)
    assert quiet_streak_alert_due(4, 4)
    assert not quiet_streak_alert_due(5, 4)
    assert not quiet_streak_alert_due(6, 4)
    assert quiet_streak_alert_due(7, 4)
    assert quiet_streak_alert_due(10, 4)
    assert not quiet_streak_alert_due(10, 0)


@pytest.mark.asyncio
async def test_alert_disabled_without_config(caplog):
    settings = Settings(alerts_enabled=False)
    delivered = await send_operator_alert(settings, "test message")
    assert delivered is False
    assert any("OPERATOR ALERT" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_alert_requires_private_chat_id():
    settings = Settings(
        alerts_enabled=True,
        telegram_bot_token="token",
        telegram_chat_id="-100public",
        alert_telegram_chat_id="",
    )
    # Public channel must never be used as the alert fallback.
    delivered = await send_operator_alert(settings, "test message")
    assert delivered is False
