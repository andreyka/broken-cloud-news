"""Workflow orchestration entry points used by scheduler/CLI.

This module provides:
- Core recurring collection/analyze jobs.
- Mode-driven publication jobs (daily, monthly, ad-hoc).
- Backward-compatible symbols used by existing imports/tests.
"""

from __future__ import annotations

from bcn.common.config import Settings
from bcn.workflows.modes.common import extract_briefing_id
from bcn.workflows.modes.regular_daily_briefing import (
    build_trigger as build_regular_briefing_trigger,
)
from bcn.workflows.modes.regular_daily_briefing import (
    run as job_publish_regular_briefing,
)
from bcn.workflows.modes.regular_monthly_newsletter import (
    build_trigger as build_regular_monthly_newsletter_trigger,
)
from bcn.workflows.modes.regular_monthly_newsletter import (
    run as job_publish_regular_monthly_newsletter,
)
from bcn.workflows.runtime import configure_runtime
from bcn.workflows.runtime import require_runtime

__all__ = [
    "configure_scheduler_runtime",
    "job_collect_ghsa",
    "job_collect_rss",
    "job_collect_twitter",
    "job_collect_reddit",
    "job_analyze_items",
    "job_publish_regular_briefing",
    "job_publish_regular_monthly_newsletter",
    "job_publish_daily_digest",
    "build_regular_briefing_trigger",
    "build_regular_monthly_newsletter_trigger",
    "extract_briefing_id",
]


def configure_scheduler_runtime(settings: Settings, sender) -> None:
    """Configure runtime dependencies used by workflow jobs."""
    configure_runtime(settings=settings, sender=sender)


async def job_collect_ghsa() -> None:
    """Scheduled job: trigger GHSA collection."""
    settings, sender = require_runtime()
    await sender(settings.collector_port, "collect_ghsa")


async def job_collect_rss() -> None:
    """Scheduled job: trigger RSS collection."""
    settings, sender = require_runtime()
    await sender(settings.collector_port, "collect_rss")


async def job_collect_twitter() -> None:
    """Scheduled job: trigger Twitter/X collection."""
    settings, sender = require_runtime()
    await sender(settings.collector_port, "collect_twitter")


async def job_collect_reddit() -> None:
    """Scheduled job: trigger Reddit collection."""
    settings, sender = require_runtime()
    await sender(settings.collector_port, "collect_reddit")


async def job_analyze_items() -> None:
    """Scheduled job: trigger item analysis."""
    settings, sender = require_runtime()
    await sender(settings.analyst_port, "analyze_new_items")


# Backward-compatible alias for older imports/tests.
job_publish_daily_digest = job_publish_regular_briefing
