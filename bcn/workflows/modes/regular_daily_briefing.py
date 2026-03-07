"""Recurring regular daily briefing workflow mode."""

from __future__ import annotations

from bcn.common.config import Settings
from bcn.workflows.modes.common import run_generation_and_distribution

MODE = "regular_daily_briefing"


def _hour_expression(settings: Settings) -> str:
    hours = settings.distribute_hours or [settings.distribute_hour]
    normalized = sorted({int(hour) for hour in hours})
    return ",".join(str(hour) for hour in normalized)


def _hour_values(settings: Settings) -> list[int]:
    hours = settings.distribute_hours or [settings.distribute_hour]
    return sorted({int(hour) for hour in hours})


def _shifted_shadow_schedule(settings: Settings) -> tuple[str, int]:
    offset_minutes = int(settings.shadow_minutes_before_publish)
    shifted: list[tuple[int, int]] = []
    for hour in _hour_values(settings):
        total_minutes = (hour * 60 + int(settings.distribute_minute) - offset_minutes) % (
            24 * 60
        )
        shifted.append((total_minutes // 60, total_minutes % 60))

    minute_values = {minute for _hour, minute in shifted}
    if len(minute_values) != 1:
        raise ValueError("Shadow schedule produced multiple minute values unexpectedly.")

    hours = ",".join(str(hour) for hour, _minute in sorted(set(shifted)))
    minute = next(iter(minute_values))
    return hours, minute


def build_trigger(settings: Settings):
    """Build the cron trigger for recurring daily briefings."""
    from apscheduler.triggers.cron import CronTrigger

    return CronTrigger(
        hour=_hour_expression(settings),
        minute=settings.distribute_minute,
        timezone=settings.distribute_timezone,
    )


def build_shadow_trigger(settings: Settings):
    """Build the cron trigger for recurring shadow evaluations."""
    from apscheduler.triggers.cron import CronTrigger

    shadow_hours, shadow_minute = _shifted_shadow_schedule(settings)
    return CronTrigger(
        hour=shadow_hours,
        minute=shadow_minute,
        timezone=settings.distribute_timezone,
    )


async def run() -> None:
    """Execute one regular daily briefing publication cycle."""
    await run_generation_and_distribution(mode=MODE)
