"""Control-plane generation service for briefing creation."""

from __future__ import annotations

import logging
import subprocess
from typing import Any
from uuid import UUID

from bcn.agents.writer.service import WriterService
from bcn.common.config import Settings
from bcn.common.db import append_generation_round
from bcn.common.db import close_pool
from bcn.common.db import create_generation_run
from bcn.common.db import finalize_generation_run
from bcn.common.db import finalize_stale_pending_generation_runs
from bcn.common.db import get_analyzed_items
from bcn.common.db import get_pool
from bcn.common.db import get_recent_briefings
from bcn.common.db import get_recent_published_items
from bcn.common.db import get_top_items_for_period
from bcn.common.db import insert_briefing
from bcn.common.db import insert_generation_preference_pair
from bcn.common.db import release_items_from_writing
from bcn.workflows.modes.common import render_writer_handoff_payload

logger = logging.getLogger(__name__)

REGULAR_DAILY_BRIEFING_MODE = "regular_daily_briefing"
REGULAR_MONTHLY_NEWSLETTER_MODE = "regular_monthly_newsletter"
_SUPPORTED_WORKFLOW_MODES = frozenset(
    (
        REGULAR_DAILY_BRIEFING_MODE,
        "ad_hoc",
        REGULAR_MONTHLY_NEWSLETTER_MODE,
    )
)


def _compose_handoff_message(
    *,
    workflow_mode: str,
    decision: str,
    briefing_id: UUID | None = None,
    item_count: int | None = None,
    human_message: str = "",
) -> str:
    """Compose one payload containing contract JSON and human-readable text."""
    payload = render_writer_handoff_payload(
        mode=workflow_mode,
        decision=decision,
        briefing_id=briefing_id,
        item_count=item_count,
    )
    text = str(human_message or "").strip()
    return payload if not text else f"{payload}\n{text}"


def _resolve_workflow_mode(mode: str) -> str:
    """Return a supported workflow mode or the regular daily default."""
    normalized = str(mode or "").strip().lower()
    if normalized in _SUPPORTED_WORKFLOW_MODES:
        return normalized
    return REGULAR_DAILY_BRIEFING_MODE


def _model_version(service: WriterService, role: str) -> str:
    """Extract a coarse model version suffix for trace metadata."""
    model = (service.llm_client.model_for_role(role) or "").strip()
    if ":" in model:
        return model.rsplit(":", 1)[-1].strip() or "unknown"
    if "@" in model:
        return model.rsplit("@", 1)[-1].strip() or "unknown"
    return "unknown"


def _component_config_snapshot(settings: Settings) -> dict[str, Any]:
    """Return a redacted config snapshot for generation trace records."""
    raw = settings.model_dump()
    filtered: dict[str, Any] = {}
    for key, value in raw.items():
        lowered = key.lower()
        if (
            any(secret in lowered for secret in ("token", "password", "webhook"))
            or lowered == "database_url"
            or lowered.startswith("smtp_")
        ):
            continue
        filtered[key] = value

    collector_keys = tuple(
        prefix for prefix in ("ghsa_", "rss_", "reddit_", "twitter_", "scrape_")
    )
    writer_keys = tuple(
        prefix
        for prefix in (
            "briefing_",
            "telegram_overflow_mode",
            "monthly_newsletter_",
        )
    )
    collector = {
        key: filtered[key] for key in filtered if key.startswith(collector_keys)
    }
    analyzer = {
        key: filtered[key]
        for key in filtered
        if key.startswith("scrape_")
        or key.startswith("llm_")
        or key in {"relevance_threshold"}
    }
    writer = {
        key: filtered[key]
        for key in filtered
        if key.startswith(writer_keys)
        or key
        in {
            "relevance_threshold",
            "briefing_lookback_hours",
            "llm_timeout",
            "comfyui_url",
            "comfyui_timeout",
            "comfyui_poll_interval",
        }
        or key.startswith("llm_")
    }
    critic = {
        key: filtered[key]
        for key in filtered
        if key.startswith("briefing_critic_") or key in {"briefing_gate_mode"}
    }
    verifier = {
        key: filtered[key]
        for key in filtered
        if key.startswith("briefing_verifier_")
    }
    return {
        "collector": collector,
        "analyzer": analyzer,
        "writer": writer,
        "critic": critic,
        "verifier": verifier,
    }


def _git_sha() -> str | None:
    """Return the current git SHA when available."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
        sha = (result.stdout or "").strip()
        return sha or None
    except Exception:
        return None


def _coerce_uuid(value: object) -> UUID | None:
    """Return a UUID instance or ``None`` when coercion fails."""
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except (TypeError, ValueError):
            return None
    return None


async def _prepare_selected_items(
    service: WriterService,
    settings: Settings,
    *,
    workflow_mode: str,
    item_dicts: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]], str | None]:
    """Return generation mode, selected items, and optional skip message."""
    if workflow_mode == REGULAR_MONTHLY_NEWSLETTER_MODE:
        selected_items = service.select_items_for_monthly_newsletter(item_dicts)
        if not selected_items:
            return (
                "monthly_newsletter",
                [],
                (
                    "Monthly newsletter skipped: not enough diverse high-signal items "
                    "after selection constraints."
                ),
            )
        return "monthly_newsletter", selected_items, None

    if bool(settings.briefing_skip_if_no_high_signal):
        high_signal = service.selector.high_signal_count(item_dicts)
        min_high_signal = max(
            1, int(settings.briefing_min_high_signal_to_publish)
        )
        if high_signal < min_high_signal:
            return (
                "standard",
                [],
                (
                    "Quiet day — not enough high-signal items "
                    f"({high_signal} < {min_high_signal}). Skipping briefing."
                ),
            )

    recent_published = await get_recent_published_items(
        hours=settings.briefing_novelty_lookback_hours,
        limit=settings.briefing_novelty_max_items,
    )
    quiet_mode = service.is_quiet_day(item_dicts)
    generation_mode = "quiet_day" if quiet_mode else "standard"
    selected_items = service.select_items_for_briefing(
        item_dicts,
        recent_published=[dict(row) for row in recent_published],
        quiet_mode=quiet_mode,
    )
    if not selected_items:
        return (
            generation_mode,
            [],
            "No items remained after quality/diversity filtering. Skipping briefing.",
        )
    return generation_mode, selected_items, None


async def _persist_generation_trace(
    *,
    run_id: UUID | None,
    candidate: dict[str, Any],
) -> None:
    """Persist side-effect-free trace artifacts produced by the writer service."""
    if run_id is None:
        return

    for round_payload in candidate.get("rounds", []) or []:
        await append_generation_round(
            run_id=run_id,
            round_index=int(round_payload.get("round_index", 0)),
            phase=str(round_payload.get("phase") or "rewrite"),
            draft_input=str(round_payload.get("draft_input") or ""),
            gate_result=(
                dict(round_payload.get("gate_result"))
                if isinstance(round_payload.get("gate_result"), dict)
                else {}
            ),
            critique_result=(
                dict(round_payload.get("critique_result"))
                if isinstance(round_payload.get("critique_result"), dict)
                else {}
            ),
            verifier_result=(
                dict(round_payload.get("verifier_result"))
                if isinstance(round_payload.get("verifier_result"), dict)
                else {}
            ),
            feedback=[str(item) for item in (round_payload.get("feedback") or [])],
            rewrite_output=(
                str(round_payload.get("rewrite_output"))
                if round_payload.get("rewrite_output") is not None
                else None
            ),
            passed=bool(round_payload.get("passed", False)),
        )

    for pair in candidate.get("preference_pairs", []) or []:
        await insert_generation_preference_pair(
            run_id=run_id,
            round_index=int(pair.get("round_index", 0)),
            chosen_text=str(pair.get("chosen_text") or ""),
            rejected_text=str(pair.get("rejected_text") or ""),
            rationale=(
                str(pair.get("rationale"))
                if pair.get("rationale") is not None
                else None
            ),
            source=str(pair.get("source") or "auto_writer_loop"),
        )


async def execute_generation(
    settings: Settings,
    *,
    mode: str,
    writer_service: WriterService | None = None,
    source: str = "workflow_service",
    manage_pool: bool = True,
) -> str:
    """Claim items, generate one briefing candidate, and persist the outcome."""
    await get_pool(settings)
    active_service = writer_service or WriterService(settings)
    owns_service = writer_service is None
    claimed_item_ids: list[UUID] = []

    try:
        stale_minutes = int(
            getattr(settings, "generation_run_stale_pending_minutes", 180)
        )
        if stale_minutes > 0:
            try:
                finalized = await finalize_stale_pending_generation_runs(
                    max_age_minutes=max(1, stale_minutes),
                    decision="BLOCKED",
                    decision_reason="writer_auto_finalize_stale_pending_run",
                )
                if finalized:
                    logger.warning(
                        "Auto-finalized %d stale PENDING generation runs before generation service execution",
                        finalized,
                    )
            except Exception:
                logger.exception("Failed to auto-finalize stale PENDING generation runs")

        workflow_mode = _resolve_workflow_mode(mode)
        if workflow_mode == REGULAR_MONTHLY_NEWSLETTER_MODE:
            items = await get_top_items_for_period(
                days=max(1, int(settings.monthly_newsletter_lookback_days)),
                min_score=max(1, int(settings.monthly_newsletter_min_score)),
                limit=max(
                    int(settings.monthly_newsletter_max_items) * 4,
                    int(settings.monthly_newsletter_min_items),
                ),
            )
        else:
            claim_limit = max(int(settings.briefing_max_items) * 8, 40)
            items = await get_analyzed_items(
                min_score=settings.relevance_threshold,
                hours=settings.briefing_lookback_hours,
                limit=claim_limit,
            )
            claimed_item_ids = [
                item_id
                for item_id in (
                    _coerce_uuid(item.get("id"))
                    for item in items
                    if str(item.get("status", "")).upper() == "WRITING"
                )
                if item_id is not None
            ]

        if not items:
            if workflow_mode == REGULAR_MONTHLY_NEWSLETTER_MODE:
                message = (
                    "Monthly newsletter skipped: no high-signal items found "
                    f"in last {settings.monthly_newsletter_lookback_days} days."
                )
            else:
                message = (
                    f"Quiet day — no items scored >= {settings.relevance_threshold} "
                    f"in the last {settings.briefing_lookback_hours}h. "
                    "Skipping briefing."
                )
            logger.info(message)
            return _compose_handoff_message(
                workflow_mode=workflow_mode,
                decision="skip",
                item_count=0,
                human_message=message,
            )

        item_dicts = [dict(item) for item in items]
        generation_mode, selected_items, skip_message = await _prepare_selected_items(
            active_service,
            settings,
            workflow_mode=workflow_mode,
            item_dicts=item_dicts,
        )
        if skip_message:
            logger.info(skip_message)
            return _compose_handoff_message(
                workflow_mode=workflow_mode,
                decision="skip",
                item_count=0,
                human_message=skip_message,
            )

        history_rows = await get_recent_briefings(limit=settings.briefing_history_items)
        history_items = [dict(row) for row in history_rows]
        trace_run_id: UUID | None = None
        candidate: dict[str, Any] | None = None
        try:
            candidate = await active_service.generate_release_candidate(
                selected_items=selected_items,
                history=history_items,
                mode=generation_mode,
            )
            trace_run_id = await create_generation_run(
                trigger_source=source,
                mode=generation_mode,
                selected_item_ids=[
                    item_id
                    for item_id in (
                        _coerce_uuid(item.get("id")) for item in selected_items
                    )
                    if item_id is not None
                ],
                selected_items=selected_items,
                llm_model=active_service.llm_client.model_for_role("writer"),
                llm_model_version=_model_version(active_service, "writer"),
                prompts=active_service.writer_llm.prompt_versions(),
                config_snapshot=_component_config_snapshot(settings),
                git_sha=_git_sha(),
                initial_draft=(
                    str(candidate["rounds"][0]["draft_input"])
                    if candidate.get("rounds")
                    else str(candidate.get("markdown") or "")
                ),
            )
            await _persist_generation_trace(run_id=trace_run_id, candidate=candidate)

            if not bool(candidate.get("release_passed", False)):
                gate = (
                    dict(candidate.get("gate"))
                    if isinstance(candidate.get("gate"), dict)
                    else {}
                )
                critique = (
                    dict(candidate.get("critique"))
                    if isinstance(candidate.get("critique"), dict)
                    else {}
                )
                verifier = (
                    dict(candidate.get("verifier"))
                    if isinstance(candidate.get("verifier"), dict)
                    else {}
                )
                message = (
                    "Blocking publish: briefing did not meet release thresholds after "
                    f"{int(candidate.get('rewrites', 0))} rewrite(s). "
                    f"gate={bool(gate.get('passed', False))} "
                    f"critic={active_service.passes_critic_thresholds(critique)} "
                    f"verifier={bool(verifier.get('passed', True))}"
                )
                logger.warning(message)
                await finalize_generation_run(
                    run_id=trace_run_id,
                    decision="BLOCKED",
                    decision_reason=message,
                    rewrite_count=int(candidate.get("rewrites", 0)),
                    final_draft=str(candidate.get("markdown") or ""),
                    final_gate=gate,
                    final_critique=critique,
                    final_verifier=verifier,
                    briefing_id=None,
                )
                return _compose_handoff_message(
                    workflow_mode=workflow_mode,
                    decision="blocked",
                    item_count=len(selected_items),
                    human_message=message,
                )

            artifact = await active_service.build_release_artifact(
                briefing_body=str(candidate.get("markdown") or ""),
                selected_items=selected_items,
                mode=generation_mode,
            )
            item_ids = [
                item_id
                for item_id in (_coerce_uuid(item.get("id")) for item in selected_items)
                if item_id is not None
            ]
            briefing_id = await insert_briefing(
                content_markdown=artifact["markdown"],
                content_html=artifact["html"],
                cover_image_url=artifact["cover_url"],
                cover_image_prompt=artifact["cover_prompt"],
                item_ids=item_ids,
            )
            await finalize_generation_run(
                run_id=trace_run_id,
                decision="PUBLISHED",
                decision_reason="release_checks_passed",
                rewrite_count=int(candidate.get("rewrites", 0)),
                final_draft=str(candidate.get("markdown") or ""),
                final_gate=dict(candidate.get("gate") or {}),
                final_critique=dict(candidate.get("critique") or {}),
                final_verifier=dict(candidate.get("verifier") or {}),
                briefing_id=briefing_id,
            )
            message = f"Briefing created: id={briefing_id} items={len(selected_items)}"
            logger.info(message)
            return _compose_handoff_message(
                workflow_mode=workflow_mode,
                decision="publish",
                briefing_id=briefing_id,
                item_count=len(selected_items),
                human_message=message,
            )
        except Exception as exc:
            logger.exception("Writer generation service failed")
            if trace_run_id is not None:
                await finalize_generation_run(
                    run_id=trace_run_id,
                    decision="BLOCKED",
                    decision_reason=f"writer_internal_error:{type(exc).__name__}",
                    rewrite_count=int(candidate.get("rewrites", 0) if candidate else 0),
                    final_draft=str(candidate.get("markdown") or "") if candidate else None,
                    final_gate=dict(candidate.get("gate") or {}) if candidate else {},
                    final_critique=(
                        dict(candidate.get("critique") or {}) if candidate else {}
                    ),
                    final_verifier=(
                        dict(candidate.get("verifier") or {}) if candidate else {}
                    ),
                    briefing_id=None,
                )
            return _compose_handoff_message(
                workflow_mode=workflow_mode,
                decision="blocked",
                item_count=len(selected_items),
                human_message="Blocking publish: internal writer error during generation.",
            )
    finally:
        if claimed_item_ids:
            try:
                await release_items_from_writing(claimed_item_ids)
            except Exception:
                logger.exception(
                    "Failed to release %d WRITING items after generation service run",
                    len(claimed_item_ids),
                )
        if owns_service:
            await active_service.close()
        if manage_pool:
            await close_pool()
