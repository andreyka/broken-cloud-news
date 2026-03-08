"""Workflow service helpers used by the CLI and daemon entrypoints."""

from __future__ import annotations

from functools import partial
import logging
from collections.abc import Callable

from bcn.common.config import Settings
from bcn.workflows.automation import build_regular_briefing_trigger
from bcn.workflows.automation import build_regular_monthly_newsletter_trigger
from bcn.workflows.automation import build_shadow_regular_briefing_trigger
from bcn.workflows.automation import configure_scheduler_runtime
from bcn.workflows.automation import job_analyze_items
from bcn.workflows.automation import job_collect_ghsa
from bcn.workflows.automation import job_collect_reddit
from bcn.workflows.automation import job_collect_rss
from bcn.workflows.automation import job_collect_twitter
from bcn.workflows.automation import (
    job_publish_regular_briefing as job_publish_daily_digest,
)
from bcn.workflows.automation import (
    job_publish_regular_monthly_newsletter as job_publish_monthly_newsletter,
)
from bcn.workflows.automation import job_shadow_regular_briefing
from bcn.workflows.distribution import execute_distribution
from bcn.workflows.generation import execute_generation_result
from bcn.workflows.modes import REGULAR_DAILY_BRIEFING_MODE
from bcn.workflows.modes import REGULAR_MONTHLY_NEWSLETTER_MODE
from bcn.workflows.modes.common import run_writer_distributor_handoff

logger = logging.getLogger("bcn")
OutputWriter = Callable[[str], None]


async def execute_workflow_mode(
    settings: Settings,
    *,
    mode: str,
) -> tuple[str, str | None]:
    """Run one workflow mode cycle without the daemon scheduler."""
    return await run_writer_distributor_handoff(
        mode=mode,
        run_generation=lambda workflow_mode: execute_generation_result(
            settings,
            mode=workflow_mode,
            source="workflow_service",
            manage_pool=True,
        ),
        run_distribution=lambda dispatch_mode, briefing_id: execute_distribution(
            settings,
            mode=dispatch_mode,
            briefing_id=briefing_id,
            manage_pool=True,
        ),
    )


async def run_daemon(
    settings: Settings,
    *,
    emit: OutputWriter | None = None,
) -> None:
    """Start the APScheduler runtime."""
    from apscheduler import AsyncScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    from bcn.common.db import close_pool
    from bcn.common.db import finalize_stale_pending_generation_runs
    from bcn.common.db import get_pool

    def _emit(message: str) -> None:
        if emit is not None:
            emit(message)

    runtime = configure_scheduler_runtime(settings)

    await get_pool(settings)
    try:
        finalized = await finalize_stale_pending_generation_runs(
            max_age_minutes=max(
                1, int(getattr(settings, "generation_run_stale_pending_minutes", 180))
            ),
            decision="BLOCKED",
            decision_reason="daemon_auto_finalize_stale_pending_run",
        )
        if finalized:
            logger.warning(
                "Auto-finalized %d stale PENDING generation runs during daemon startup",
                finalized,
            )
    except Exception:
        logger.exception("Failed to auto-finalize stale PENDING generation runs")

    _emit("Starting Broken Cloud News scheduler...")
    try:
        async with AsyncScheduler() as scheduler:
            await scheduler.add_schedule(
                partial(job_collect_ghsa, runtime),
                IntervalTrigger(hours=settings.ghsa_interval_hours),
                id="ghsa_collector",
            )
            await scheduler.add_schedule(
                partial(job_collect_rss, runtime),
                IntervalTrigger(hours=settings.rss_interval_hours),
                id="rss_collector",
            )
            await scheduler.add_schedule(
                partial(job_collect_reddit, runtime),
                IntervalTrigger(hours=settings.reddit_interval_hours),
                id="reddit_collector",
            )
            await scheduler.add_schedule(
                partial(job_collect_twitter, runtime),
                IntervalTrigger(hours=settings.twitter_interval_hours),
                id="twitter_collector",
            )
            await scheduler.add_schedule(
                partial(job_analyze_items, runtime),
                IntervalTrigger(minutes=settings.analyst_interval_minutes),
                id="analyst",
            )

            if settings.shadow_enabled:
                await scheduler.add_schedule(
                    partial(job_shadow_regular_briefing, runtime),
                    build_shadow_regular_briefing_trigger(settings),
                    id=f"{REGULAR_DAILY_BRIEFING_MODE}_shadow",
                )

            await scheduler.add_schedule(
                partial(job_publish_daily_digest, runtime),
                build_regular_briefing_trigger(settings),
                id=REGULAR_DAILY_BRIEFING_MODE,
            )
            if settings.monthly_newsletter_enabled:
                await scheduler.add_schedule(
                    partial(job_publish_monthly_newsletter, runtime),
                    build_regular_monthly_newsletter_trigger(settings),
                    id=REGULAR_MONTHLY_NEWSLETTER_MODE,
                )

            _emit("Scheduler started. Press Ctrl+C to stop.")
            await scheduler.run_until_stopped()
    finally:
        try:
            await close_pool()
        except Exception:
            logger.exception("Failed to close DB pool during daemon shutdown")
