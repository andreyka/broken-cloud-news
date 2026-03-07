"""Workflow orchestration entry points used by scheduler/CLI.

This module provides:
- Core recurring collection/analyze jobs.
- Mode-driven publication jobs (daily, monthly, ad-hoc).
- Backward-compatible symbols used by existing imports/tests.
"""

from __future__ import annotations

import logging
from pathlib import Path

from bcn.common.config import Settings
from bcn.workflows.modes import REGULAR_DAILY_BRIEFING_MODE
from bcn.workflows.modes.common import extract_briefing_id
from bcn.workflows.modes.regular_daily_briefing import (
    build_trigger as build_regular_briefing_trigger,
)
from bcn.workflows.modes.regular_daily_briefing import (
    build_shadow_trigger as build_shadow_regular_briefing_trigger,
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

logger = logging.getLogger(__name__)

__all__ = [
    "configure_scheduler_runtime",
    "job_collect_ghsa",
    "job_collect_rss",
    "job_collect_twitter",
    "job_collect_reddit",
    "job_analyze_items",
    "job_publish_regular_briefing",
    "job_publish_regular_monthly_newsletter",
    "job_shadow_regular_briefing",
    "job_publish_daily_digest",
    "build_regular_briefing_trigger",
    "build_shadow_regular_briefing_trigger",
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


async def job_shadow_regular_briefing() -> None:
    """Scheduled job: run shadow evaluation before the regular briefing slot."""
    settings, _sender = require_runtime()
    if not bool(settings.shadow_enabled):
        logger.info("Shadow scheduler triggered while disabled; skipping.")
        return

    overrides_path = str(settings.shadow_candidate_overrides_path or "").strip() or None
    if not overrides_path:
        logger.info(
            "Shadow evaluation skipped: BCN_SHADOW_CANDIDATE_OVERRIDES_PATH is not configured."
        )
        return
    if not Path(overrides_path).exists():
        logger.warning(
            "Shadow evaluation skipped: candidate overrides file not found: %s",
            overrides_path,
        )
        return

    from bcn.common.db import ensure_evaluation_tables
    from bcn.common.db import complete_evaluation_run
    from bcn.common.db import create_evaluation_run
    from bcn.common.db import fail_evaluation_run
    from bcn.evaluation import run_shadow_lane

    await ensure_evaluation_tables()
    run_id = await create_evaluation_run(
        lane="shadow",
        source="scheduler",
        workflow_mode=REGULAR_DAILY_BRIEFING_MODE,
        notes="Scheduled pre-publish shadow evaluation.",
    )
    try:
        report = await run_shadow_lane(
            settings,
            workflow_mode=REGULAR_DAILY_BRIEFING_MODE,
            candidate_overrides_path=overrides_path,
            include_text=bool(settings.shadow_include_text),
        )
        await complete_evaluation_run(
            run_id,
            report,
            notes="Scheduled pre-publish shadow evaluation.",
        )
        summary = report.get("summary") if isinstance(report, dict) else {}
        if not isinstance(summary, dict):
            summary = {}
        logger.info(
            "Stored scheduled shadow evaluation run_id=%s recommendation=%s confidence=%s item_pool=%s",
            run_id,
            summary.get("recommendation", "hold"),
            summary.get("confidence", "low"),
            report.get("item_pool_count", 0),
        )
    except Exception as exc:
        await fail_evaluation_run(run_id, error_message=str(exc))
        raise


# Backward-compatible alias for older imports/tests.
job_publish_daily_digest = job_publish_regular_briefing
