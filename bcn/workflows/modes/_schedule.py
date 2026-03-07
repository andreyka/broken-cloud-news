"""Helpers for recurring workflow scheduling."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


def schedule_start_time(timezone_name: str) -> datetime:
    """Anchor cron schedules in their own timezone.

    APScheduler 4 alpha derives a UTC ``start_time`` when omitted, which can skip
    the first local run after a restart for non-UTC cron schedules.
    """

    return datetime.now(ZoneInfo(timezone_name))
