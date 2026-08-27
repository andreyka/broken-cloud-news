"""Workflow orchestration entry points used by scheduler/CLI.

This module provides:
- Core recurring collection/analyze jobs.
- Mode-driven publication jobs (daily, monthly, ad-hoc).
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

from bcn.common.config import Settings
from bcn.evaluation.lanes import load_settings_with_overrides
from bcn.persistence.evaluation import complete_evaluation_run
from bcn.persistence.evaluation import create_evaluation_run
from bcn.persistence.training import get_distributed_briefings_without_ai_review
from bcn.workflows.analysis import execute_analysis
from bcn.workflows.ai_review import get_ai_review_config
from bcn.workflows.collection import execute_collection
from bcn.contracts.modes import REGULAR_DAILY_BRIEFING_MODE
from bcn.workflows.modes.common import extract_briefing_id
from bcn.workflows.modes._schedule import schedule_start_time
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
    "execute_scheduled_analysis",
    "execute_scheduled_ai_review_backfill",
    "execute_scheduled_collection",
    "execute_shadow_regular_briefing",
    "build_ai_review_backfill_trigger",
    "job_collect_ghsa",
    "job_collect_rss",
    "job_collect_twitter",
    "job_collect_reddit",
    "job_analyze_items",
    "job_backfill_ai_reviews",
    "job_publish_regular_briefing",
    "job_publish_regular_monthly_newsletter",
    "job_shadow_regular_briefing",
    "build_regular_briefing_trigger",
    "build_shadow_regular_briefing_trigger",
    "build_regular_monthly_newsletter_trigger",
    "extract_briefing_id",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _llm_provider_for_role(settings: Settings, role: str) -> str:
    override = str(getattr(settings, f"llm_provider_{role}", "") or "").strip()
    return override or str(settings.llm_provider or "").strip()


def _llm_base_url_for_role(settings: Settings, role: str) -> str:
    override = str(getattr(settings, f"llm_base_url_{role}", "") or "").strip()
    return override or str(settings.llm_base_url or "").strip()


def build_ai_review_backfill_trigger(settings: Settings):
    """Return the weekly trigger used to backfill missing AI reviews."""
    from apscheduler.triggers.cron import CronTrigger

    return CronTrigger(
        day_of_week=str(settings.ai_review_backfill_weekday or "sun").strip().lower(),
        hour=int(settings.ai_review_backfill_hour),
        minute=int(settings.ai_review_backfill_minute),
        timezone=settings.ai_review_backfill_timezone,
        start_time=schedule_start_time(settings.ai_review_backfill_timezone),
    )


async def _probe_openai_compat_endpoint(base_url: str) -> str | None:
    target = urljoin(base_url.rstrip("/") + "/", "models")
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            response = await client.get(target)
    except Exception as exc:
        return f"{target} unreachable ({type(exc).__name__}: {exc})"

    if response.status_code >= 500:
        return f"{target} returned {response.status_code}"
    return None


async def _shadow_candidate_endpoint_error(
    candidate_settings: Settings,
    champion_settings: Settings,
) -> str | None:
    # Only probe endpoints the overrides introduce: champion endpoints are
    # already exercised by production, and external ones sit behind the
    # egress proxy that this deliberately proxy-less probe cannot traverse.
    champion_urls = {
        _llm_base_url_for_role(champion_settings, role)
        for role in ("writer", "critic", "verifier")
    }
    champion_urls.add(str(champion_settings.llm_base_url or "").strip())
    checked_urls: set[str] = set()
    for role in ("writer", "critic", "verifier"):
        if _llm_provider_for_role(candidate_settings, role) != "openai_compat":
            continue
        base_url = _llm_base_url_for_role(candidate_settings, role)
        if not base_url or base_url in checked_urls or base_url in champion_urls:
            continue
        checked_urls.add(base_url)
        error = await _probe_openai_compat_endpoint(base_url)
        if error:
            return error
    return None


async def _store_shadow_unavailable_run(
    *,
    workflow_mode: str,
    candidate_overrides: dict[str, Any],
    notes: str,
    reason: str,
) -> None:
    run_id = await create_evaluation_run(
        lane="shadow",
        source="scheduler",
        workflow_mode=workflow_mode,
        candidate_overrides=candidate_overrides,
        notes=notes,
    )
    report = {
        "generated_at": _now_iso(),
        "lane": "shadow",
        "workflow_mode": workflow_mode,
        "candidate_overrides": candidate_overrides,
        "item_pool_count": 0,
        "selection_overlap_ratio": 0.0,
        "champion": {
            "decision": "skipped",
            "reason": "shadow_candidate_unavailable",
        },
        "candidate": {
            "decision": "unavailable",
            "reason": reason,
        },
        "summary": {
            "recommendation": "unavailable",
            "confidence": "low",
            "selection_overlap_ratio": 0.0,
            "reason": reason,
        },
    }
    await complete_evaluation_run(run_id, report, report_path=None, notes=notes)
    logger.info(
        "Stored scheduled shadow evaluation run_id=%s recommendation=unavailable confidence=low item_pool=0",
        run_id,
    )


def configure_scheduler_runtime(
    settings: Settings,
) -> WorkflowRuntime:
    """Configure runtime dependencies used by workflow jobs."""
    return build_workflow_runtime(settings=settings)


async def execute_scheduled_collection(
    runtime: WorkflowRuntime,
    *,
    source: str,
) -> None:
    """Run one scheduled collection step from typed workflow metadata."""
    await execute_collection(
        runtime.settings,
        source=source,
        origin="scheduler",
        manage_pool=False,
    )


async def job_collect_ghsa(runtime: WorkflowRuntime) -> None:
    """Scheduled job: trigger GHSA collection."""
    await execute_scheduled_collection(runtime, source="ghsa")


async def job_collect_rss(runtime: WorkflowRuntime) -> None:
    """Scheduled job: trigger RSS collection."""
    await execute_scheduled_collection(runtime, source="rss")


async def job_collect_twitter(runtime: WorkflowRuntime) -> None:
    """Scheduled job: trigger Twitter/X collection."""
    await execute_scheduled_collection(runtime, source="twitter")


async def job_collect_reddit(runtime: WorkflowRuntime) -> None:
    """Scheduled job: trigger Reddit collection."""
    await execute_scheduled_collection(runtime, source="reddit")


async def execute_scheduled_analysis(runtime: WorkflowRuntime) -> None:
    """Run the scheduled analyst step from typed workflow metadata."""
    await execute_analysis(
        runtime.settings,
        source="scheduler",
        manage_pool=False,
    )


async def job_analyze_items(runtime: WorkflowRuntime) -> None:
    """Scheduled job: trigger item analysis."""
    await execute_scheduled_analysis(runtime)


async def execute_scheduled_ai_review_backfill(runtime: WorkflowRuntime) -> None:
    """Queue AI reviews for distributed briefings that still lack them."""
    from bcn.workflows.queue import enqueue_ai_review_job

    settings = runtime.settings
    if not bool(settings.ai_review_backfill_enabled):
        logger.info("AI review backfill scheduler triggered while disabled; skipping.")
        return

    config = get_ai_review_config(settings)
    if not bool(settings.ai_review_auto_enabled) or not config.enabled:
        logger.info("AI review backfill skipped: AI review backend is not configured.")
        return

    backlog = await get_distributed_briefings_without_ai_review(
        limit=int(settings.ai_review_backfill_max_briefings),
        source="auto_distribution",
    )
    if not backlog:
        logger.info("AI review backfill found no missing distributed briefings.")
        return

    queued = 0
    for row in backlog:
        briefing_id = row["id"]
        job_id = await enqueue_ai_review_job(
            settings,
            briefing_id=briefing_id,
            source="auto_distribution",
            notes="Queued by weekly scheduled AI review backfill.",
        )
        if job_id is not None:
            queued += 1
    logger.info(
        "AI review backfill scanned=%s queued=%s source=auto_distribution",
        len(backlog),
        queued,
    )


async def job_backfill_ai_reviews(runtime: WorkflowRuntime) -> None:
    """Scheduled job: queue weekly backfill AI reviews for distributed briefings."""
    await execute_scheduled_ai_review_backfill(runtime)


async def execute_shadow_regular_briefing(runtime: WorkflowRuntime) -> None:
    """Run the scheduled shadow evaluation step from typed workflow metadata."""
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

    candidate_settings, candidate_overrides = load_settings_with_overrides(
        settings,
        overrides_path,
    )
    endpoint_error = await _shadow_candidate_endpoint_error(
        candidate_settings, settings
    )
    if endpoint_error:
        reason = f"candidate_endpoint_unavailable: {endpoint_error}"
        logger.warning("Shadow evaluation skipped: %s", reason)
        await _store_shadow_unavailable_run(
            workflow_mode=REGULAR_DAILY_BRIEFING_MODE,
            candidate_overrides=candidate_overrides,
            notes="Scheduled pre-publish shadow evaluation.",
            reason=reason,
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


async def job_shadow_regular_briefing(runtime: WorkflowRuntime) -> None:
    """Scheduled job: run shadow evaluation before the regular briefing slot."""
    await execute_shadow_regular_briefing(runtime)
