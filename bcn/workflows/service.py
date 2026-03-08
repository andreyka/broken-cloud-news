"""Workflow service helpers used by the CLI and daemon entrypoints."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any

from bcn.common.agent_client import AgentClient
from bcn.common.agent_client import build_port_sender_agent_client
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

AgentRunner = Callable[[type, Settings, str], Awaitable[str]]
AgentSender = Callable[[int, str], Awaitable[str]]
OutputWriter = Callable[[str], None]


async def execute_workflow_mode(
    settings: Settings,
    *,
    mode: str,
    agent_client: AgentClient | None = None,
    run_agent_directly: AgentRunner | None = None,
) -> tuple[str, str | None]:
    """Run one workflow mode cycle without the daemon scheduler."""
    del agent_client
    del run_agent_directly

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
    agent_client: AgentClient | None = None,
    sender: AgentSender | None = None,
    emit: OutputWriter | None = None,
) -> None:
    """Start all A2A agents and the APScheduler runtime."""
    from apscheduler import AsyncScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    from bcn.agents.analyst.agent import AnalystExecutor
    from bcn.agents.analyst.agent import SKILLS as ANAL_SKILLS
    from bcn.agents.base import build_agent_card
    from bcn.agents.base import serve_agent
    from bcn.agents.collector.agent import CollectorExecutor
    from bcn.agents.collector.agent import SKILLS as COLL_SKILLS
    from bcn.agents.critic.agent import CriticExecutor
    from bcn.agents.critic.agent import SKILLS as CRIT_SKILLS
    from bcn.agents.verifier.agent import SKILLS as VERI_SKILLS
    from bcn.agents.verifier.agent import VerifierExecutor
    from bcn.agents.writer.agent import SKILLS as WRIT_SKILLS
    from bcn.agents.writer.agent import WriterExecutor
    from bcn.common.db import close_pool
    from bcn.common.db import finalize_stale_pending_generation_runs
    from bcn.common.db import get_pool

    def _emit(message: str) -> None:
        if emit is not None:
            emit(message)

    runtime_agent_client = agent_client
    if runtime_agent_client is None:
        if sender is not None:
            runtime_agent_client = build_port_sender_agent_client(
                settings,
                sender=sender,
            )
        else:
            from bcn.common.agent_runtime import send_to_agent

            async def _local_sender(port: int, skill: str) -> str:
                return await send_to_agent(
                    port,
                    skill,
                    timeout_seconds=settings.a2a_request_timeout_seconds,
                )

            runtime_agent_client = build_port_sender_agent_client(
                settings,
                sender=_local_sender,
            )

    configure_scheduler_runtime(settings, agent_client=runtime_agent_client)

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

    _emit("Starting Broken Cloud News agents...")

    collector_card = build_agent_card(
        "BCN Collector",
        "Collects cloud security news from GHSA, Twitter, RSS, Reddit",
        f"http://localhost:{settings.collector_port}/",
        COLL_SKILLS,
    )
    analyst_card = build_agent_card(
        "BCN Analyst",
        "Analyzes news items for relevance scoring",
        f"http://localhost:{settings.analyst_port}/",
        ANAL_SKILLS,
    )
    writer_card = build_agent_card(
        "BCN Writer",
        "Generates security briefings with cover images",
        f"http://localhost:{settings.writer_port}/",
        WRIT_SKILLS,
    )
    critic_card = build_agent_card(
        "BCN Critic",
        "Critiques briefing quality and provides recommendations",
        f"http://localhost:{settings.critic_port}/",
        CRIT_SKILLS,
    )
    verifier_card = build_agent_card(
        "BCN Verifier",
        "Verifies briefing facts, links, and top-story quality",
        f"http://localhost:{settings.verifier_port}/",
        VERI_SKILLS,
    )

    collector_exec = CollectorExecutor(settings)
    analyst_exec = AnalystExecutor(settings)
    writer_exec = WriterExecutor(settings)
    critic_exec = CriticExecutor(settings)
    verifier_exec = VerifierExecutor(settings)
    executors = [
        collector_exec,
        analyst_exec,
        writer_exec,
        critic_exec,
        verifier_exec,
    ]

    tasks: list[asyncio.Task[Any]] = []

    _emit(f"  Collector on :{settings.collector_port}")
    _emit(f"  Analyst  on :{settings.analyst_port}")
    _emit(f"  Writer   on :{settings.writer_port}")
    _emit(f"  Critic on :{settings.critic_port}")
    _emit(f"  Verifier on :{settings.verifier_port}")

    try:
        tasks = [
            asyncio.create_task(
                serve_agent(collector_card, collector_exec, settings.collector_port)
            ),
            asyncio.create_task(
                serve_agent(analyst_card, analyst_exec, settings.analyst_port)
            ),
            asyncio.create_task(
                serve_agent(writer_card, writer_exec, settings.writer_port)
            ),
            asyncio.create_task(
                serve_agent(critic_card, critic_exec, settings.critic_port)
            ),
            asyncio.create_task(
                serve_agent(verifier_card, verifier_exec, settings.verifier_port)
            ),
        ]

        async with AsyncScheduler() as scheduler:
            await scheduler.add_schedule(
                job_collect_ghsa,
                IntervalTrigger(hours=settings.ghsa_interval_hours),
                id="ghsa_collector",
            )
            await scheduler.add_schedule(
                job_collect_rss,
                IntervalTrigger(hours=settings.rss_interval_hours),
                id="rss_collector",
            )
            await scheduler.add_schedule(
                job_collect_reddit,
                IntervalTrigger(hours=settings.reddit_interval_hours),
                id="reddit_collector",
            )
            await scheduler.add_schedule(
                job_collect_twitter,
                IntervalTrigger(hours=settings.twitter_interval_hours),
                id="twitter_collector",
            )
            await scheduler.add_schedule(
                job_analyze_items,
                IntervalTrigger(minutes=settings.analyst_interval_minutes),
                id="analyst",
            )

            if settings.shadow_enabled:
                await scheduler.add_schedule(
                    job_shadow_regular_briefing,
                    build_shadow_regular_briefing_trigger(settings),
                    id=f"{REGULAR_DAILY_BRIEFING_MODE}_shadow",
                )

            await scheduler.add_schedule(
                job_publish_daily_digest,
                build_regular_briefing_trigger(settings),
                id=REGULAR_DAILY_BRIEFING_MODE,
            )
            if settings.monthly_newsletter_enabled:
                await scheduler.add_schedule(
                    job_publish_monthly_newsletter,
                    build_regular_monthly_newsletter_trigger(settings),
                    id=REGULAR_MONTHLY_NEWSLETTER_MODE,
                )

            await scheduler.start_in_background()
            _emit("Scheduler started. Press Ctrl+C to stop.")
            await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        for executor in executors:
            close_fn = getattr(executor, "close", None)
            if callable(close_fn):
                try:
                    maybe = close_fn()
                    if hasattr(maybe, "__await__"):
                        await maybe
                except Exception:
                    logger.exception(
                        "Failed to close %s executor", executor.__class__.__name__
                    )
        try:
            await close_pool()
        except Exception:
            logger.exception("Failed to close DB pool during daemon shutdown")
