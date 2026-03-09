"""Workflow orchestration entry points used by scheduler/CLI.

This module provides:
- Core recurring collection/analyze jobs.
- Mode-driven publication jobs (daily, monthly, ad-hoc).
- Backward-compatible symbols used by existing imports/tests.
"""

from __future__ import annotations

import logging
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

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
from bcn.workflows.runtime import build_workflow_runtime
from bcn.workflows.runtime import WorkflowRuntime

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
) -> WorkflowRuntime:
    """Configure runtime dependencies used by workflow jobs."""
    return build_workflow_runtime(settings=settings)


async def _run_job_safely(name: str, coro: Coroutine[Any, Any, None]) -> None:
    """Execute a scheduled job with error isolation.

    Catches and logs any exception so that a single failing job does not
    crash the entire scheduler process.
    """
    try:
        await coro
    except Exception:
        logger.exception("Scheduled job '%s' failed", name)


async def job_collect_ghsa(runtime: WorkflowRuntime) -> None:
    """Scheduled job: trigger GHSA collection."""
    await _run_job_safely(
        "ghsa_collector",
        execute_collection(
            runtime.settings,
            source="ghsa",
            origin="scheduler",
            manage_pool=False,
        ),
    )


async def job_collect_rss(runtime: WorkflowRuntime) -> None:
    """Scheduled job: trigger RSS collection."""
    await _run_job_safely(
        "rss_collector",
        execute_collection(
            runtime.settings,
            source="rss",
            origin="scheduler",
            manage_pool=False,
        ),
    )


async def job_collect_twitter(runtime: WorkflowRuntime) -> None:
    """Scheduled job: trigger Twitter/X collection."""
    await _run_job_safely(
        "twitter_collector",
        execute_collection(
            runtime.settings,
            source="twitter",
            origin="scheduler",
            manage_pool=False,
        ),
    )


async def job_collect_reddit(runtime: WorkflowRuntime) -> None:
    """Scheduled job: trigger Reddit collection."""
    await _run_job_safely(
        "reddit_collector",
        execute_collection(
            runtime.settings,
            source="reddit",
            origin="scheduler",
            manage_pool=False,
        ),
    )


async def job_analyze_items(runtime: WorkflowRuntime) -> None:
    """Scheduled job: trigger item analysis."""
    await _run_job_safely(
        "analyst",
        execute_analysis(
            runtime.settings,
            source="scheduler",
            manage_pool=False,
        ),
    )


async def job_shadow_regular_briefing(runtime: WorkflowRuntime) -> None:
    """Scheduled job: run shadow evaluation before the regular briefing slot."""
    settings = runtime.settings
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

    await _run_job_safely(
        "shadow_regular_briefing",
        execute_shadow_lane(
            settings,
            workflow_mode=REGULAR_DAILY_BRIEFING_MODE,
            candidate_overrides_path=overrides_path,
            output_path=None,
            include_text=bool(settings.shadow_include_text),
            store_db=True,
            source="scheduler",
            notes="Scheduled pre-publish shadow evaluation.",
            manage_pool=False,
        ),
    )
    # Note: logging of the shadow result is handled within execute_shadow_lane
    # or by the caller in mode-specific jobs.


# Backward-compatible alias for older imports/tests.
job_publish_daily_digest = job_publish_regular_briefing
