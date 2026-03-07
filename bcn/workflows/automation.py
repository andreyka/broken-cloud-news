"""Workflow orchestration entry points used by scheduler/CLI.

This module provides:
- Core recurring collection/analyze jobs.
- Mode-driven publication jobs (daily, monthly, ad-hoc).
- Backward-compatible symbols used by existing imports/tests.
"""

from __future__ import annotations

import logging
from pathlib import Path

from bcn.common.agent_client import AgentClient
from bcn.common.config import Settings
from bcn.workflows.analysis import execute_analysis
from bcn.workflows.collection import execute_collection
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


def configure_scheduler_runtime(
    settings: Settings,
    sender=None,
    *,
    agent_client: AgentClient | None = None,
) -> None:
    """Configure runtime dependencies used by workflow jobs."""
    configure_runtime(settings=settings, agent_client=agent_client, sender=sender)


async def job_collect_ghsa() -> None:
    """Scheduled job: trigger GHSA collection."""
    settings, _agent_client = require_runtime()
    await execute_collection(
        settings,
        source="ghsa",
        origin="scheduler",
        manage_pool=False,
    )


async def job_collect_rss() -> None:
    """Scheduled job: trigger RSS collection."""
    settings, _agent_client = require_runtime()
    await execute_collection(
        settings,
        source="rss",
        origin="scheduler",
        manage_pool=False,
    )


async def job_collect_twitter() -> None:
    """Scheduled job: trigger Twitter/X collection."""
    settings, _agent_client = require_runtime()
    await execute_collection(
        settings,
        source="twitter",
        origin="scheduler",
        manage_pool=False,
    )


async def job_collect_reddit() -> None:
    """Scheduled job: trigger Reddit collection."""
    settings, _agent_client = require_runtime()
    await execute_collection(
        settings,
        source="reddit",
        origin="scheduler",
        manage_pool=False,
    )


async def job_analyze_items() -> None:
    """Scheduled job: trigger item analysis."""
    settings, _agent_client = require_runtime()
    await execute_analysis(
        settings,
        source="scheduler",
        manage_pool=False,
    )


async def job_shadow_regular_briefing() -> None:
    """Scheduled job: run shadow evaluation before the regular briefing slot."""
    settings, _agent_client = require_runtime()
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

    from bcn.evaluation.service import execute_shadow_lane

    report = await execute_shadow_lane(
        settings,
        workflow_mode=REGULAR_DAILY_BRIEFING_MODE,
        candidate_overrides_path=overrides_path,
        output_path=None,
        include_text=bool(settings.shadow_include_text),
        store_db=True,
        source="scheduler",
        notes="Scheduled pre-publish shadow evaluation.",
        manage_pool=False,
    )
    summary = report.get("summary") if isinstance(report, dict) else {}
    if not isinstance(summary, dict):
        summary = {}
    logger.info(
        "Stored scheduled shadow evaluation run_id=%s recommendation=%s confidence=%s item_pool=%s",
        report.get("db_run_id", "unknown"),
        summary.get("recommendation", "hold"),
        summary.get("confidence", "low"),
        report.get("item_pool_count", 0),
    )


# Backward-compatible alias for older imports/tests.
job_publish_daily_digest = job_publish_regular_briefing
