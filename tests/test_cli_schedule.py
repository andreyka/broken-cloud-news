from __future__ import annotations

from bcn.cli import _build_daily_digest_trigger
from bcn.common.config import Settings


def test_build_daily_digest_trigger_from_multi_hours():
    settings = Settings(
        distribute_hours=[19, 9, 13, 9],
        distribute_minute=0,
        distribute_timezone="UTC",
    )
    trigger = _build_daily_digest_trigger(settings)
    assert trigger.hour == "9,13,19"
    assert trigger.minute == 0
    assert str(trigger.timezone) == "UTC"


def test_build_daily_digest_trigger_legacy_fallback_hour():
    settings = Settings(
        distribute_hour=9,
        distribute_hours=[],
        distribute_minute=15,
        distribute_timezone="America/Los_Angeles",
    )
    trigger = _build_daily_digest_trigger(settings)
    assert trigger.hour == "9"
    assert trigger.minute == 15
    assert str(trigger.timezone) == "America/Los_Angeles"
