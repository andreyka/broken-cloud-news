"""Workflow service helpers used by the CLI and daemon entrypoints."""

from __future__ import annotations

import asyncio
from functools import partial
import logging
import signal
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


async def _start_health_server(port: int) -> asyncio.AbstractServer:
    """Start a minimal HTTP health-check server for container probes."""

    async def _handle(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            await reader.readline()  # consume request line
            body = b'{"ok":true}'
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"\r\n" + body
            )
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(_handle, "0.0.0.0", port)
    logger.info("Health check server listening on port %d", port)
    return server


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
    """Start the APScheduler runtime with graceful SIGTERM handling."""
    from apscheduler import AsyncScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    from bcn.persistence.runtime import close_pool
    from bcn.persistence.runtime import get_pool
    from bcn.persistence.training import finalize_stale_pending_generation_runs

    def _emit(message: str) -> None:
        if emit is not None:
            emit(message)

    runtime = configure_scheduler_runtime(settings)

    await get_pool(settings)

    health_server: asyncio.AbstractServer | None = None
    if settings.health_check_port > 0:
        try:
            health_server = await _start_health_server(settings.health_check_port)
        except Exception:
            logger.exception("Failed to start health check server")

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
            # Register SIGTERM handler for graceful container shutdown
            shutdown_event = asyncio.Event()
            loop = asyncio.get_running_loop()

            def _handle_sigterm() -> None:
                logger.info("Received SIGTERM, initiating graceful shutdown...")
                shutdown_event.set()

            try:
                loop.add_signal_handler(signal.SIGTERM, _handle_sigterm)
            except NotImplementedError:
                pass  # Windows does not support add_signal_handler

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

            # Run the scheduler and wait for SIGTERM concurrently
            scheduler_task = asyncio.create_task(scheduler.run_until_stopped())
            shutdown_task = asyncio.create_task(shutdown_event.wait())
            done, pending = await asyncio.wait(
                {scheduler_task, shutdown_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            logger.info("Shutting down scheduler...")
    finally:
        if health_server is not None:
            health_server.close()
            await health_server.wait_closed()
        try:
            await close_pool()
        except Exception:
            logger.exception("Failed to close DB pool during daemon shutdown")
