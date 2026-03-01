"""Recurring monthly newsletter workflow mode."""

from __future__ import annotations

from bcn.common.config import Settings
from bcn.workflows.modes.common import run_generation_and_distribution

MODE = "regular_monthly_newsletter"


def build_trigger(settings: Settings):
    """Build the cron trigger for recurring monthly newsletter publication."""
    from apscheduler.triggers.cron import CronTrigger

    return CronTrigger(
        day=settings.monthly_newsletter_day,
        hour=settings.monthly_newsletter_hour,
        minute=settings.monthly_newsletter_minute,
        timezone=settings.monthly_newsletter_timezone,
    )


async def run() -> None:
    """Execute one regular monthly newsletter publication cycle."""
    await run_generation_and_distribution(mode=MODE)

