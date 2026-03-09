"""Workflow service helpers used by the CLI and daemon entrypoints."""

from __future__ import annotations

from functools import partial
import logging
from collections.abc import Callable

from bcn.common.config import Settings
from bcn.workflows.automation import configure_scheduler_runtime
from bcn.workflows.catalog import iter_scheduled_workflows
from bcn.workflows.distribution import execute_distribution
from bcn.workflows.generation import execute_generation_result
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

    from bcn.persistence.runtime import close_pool
    from bcn.persistence.runtime import get_pool
    from bcn.persistence.training import finalize_stale_pending_generation_runs

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
            for definition in iter_scheduled_workflows(settings):
                await scheduler.add_schedule(
                    partial(definition.execute, runtime),
                    definition.build_trigger(settings),
                    id=definition.workflow_id,
                )

            _emit("Scheduler started. Press Ctrl+C to stop.")
            await scheduler.run_until_stopped()
    finally:
        try:
            await close_pool()
        except Exception:
            logger.exception("Failed to close DB pool during daemon shutdown")
