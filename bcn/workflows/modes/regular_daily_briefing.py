"""Recurring regular daily briefing workflow mode."""

from __future__ import annotations

from bcn.common.config import Settings
from bcn.workflows.modes.common import run_generation_and_distribution

MODE = "regular_daily_briefing"


def _hour_expression(settings: Settings) -> str:
    hours = settings.distribute_hours or [settings.distribute_hour]
    normalized = sorted({int(hour) for hour in hours})
    return ",".join(str(hour) for hour in normalized)


def build_trigger(settings: Settings):
    """Build the cron trigger for recurring daily briefings."""
    from apscheduler.triggers.cron import CronTrigger

    return CronTrigger(
        hour=_hour_expression(settings),
        minute=settings.distribute_minute,
        timezone=settings.distribute_timezone,
    )


async def run() -> None:
    """Execute one regular daily briefing publication cycle."""
    await run_generation_and_distribution(mode=MODE)

