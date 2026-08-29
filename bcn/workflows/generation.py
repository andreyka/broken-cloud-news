"""Control-plane generation service for briefing creation."""

from __future__ import annotations

import logging
import subprocess
from typing import Any
from uuid import UUID

from bcn.common.config import Settings
from bcn.contracts.modes import ALL_WORKFLOW_MODES
from bcn.contracts.modes import REGULAR_DAILY_BRIEFING_MODE
from bcn.contracts.modes import REGULAR_MONTHLY_NEWSLETTER_MODE
from bcn.contracts.services import WriterWorkflow
from bcn.contracts.workflow import WriterHandoff
from bcn.contracts.workflow import WriterHandoffResult
from bcn.contracts.workflow import render_writer_handoff_message
from bcn.persistence.briefings import get_recent_briefings
from bcn.persistence.briefings import insert_briefing
from bcn.persistence.news_items import get_analyzed_items
from bcn.persistence.news_items import get_recent_published_items
from bcn.persistence.news_items import get_top_items_for_period
from bcn.persistence.news_items import preview_generation_candidate_pool
from bcn.persistence.news_items import release_items_from_writing
from bcn.persistence.runtime import close_pool
from bcn.persistence.runtime import get_pool
from bcn.persistence.training import append_generation_round
from bcn.persistence.training import create_generation_run
from bcn.persistence.training import finalize_generation_run
from bcn.persistence.training import finalize_stale_pending_generation_runs
from bcn.persistence.training import insert_generation_preference_pair
from bcn.persistence.training import insert_ai_review
from bcn.service_registry import build_writer_workflow
from bcn.services.writer.selection import build_selection_trace
from bcn.workflows.ai_review import run_prepublish_ai_review_gate

logger = logging.getLogger(__name__)

_SUPPORTED_WORKFLOW_MODES = frozenset(ALL_WORKFLOW_MODES)


def _resolve_workflow_mode(mode: str) -> str:
    """Return a supported workflow mode or the regular daily default."""
    normalized = str(mode or "").strip().lower()
    if normalized in _SUPPORTED_WORKFLOW_MODES:
        return normalized
    return REGULAR_DAILY_BRIEFING_MODE


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
    for key in ("collector_service_url", "service_request_timeout_seconds"):
        if key in filtered:
            collector[key] = filtered[key]
    analyzer = {
        key: filtered[key]
        for key in filtered
        if key.startswith("scrape_")
        or key.startswith("llm_")
        or key in {"relevance_threshold"}
    }
    for key in ("analyst_service_url", "service_request_timeout_seconds"):
        if key in filtered:
            analyzer[key] = filtered[key]
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
            "writer_service_url",
            "service_request_timeout_seconds",
        }
        or key.startswith("llm_")
    }
    critic = {
        key: filtered[key]
        for key in filtered
        if key.startswith("briefing_critic_")
        or key in {"briefing_gate_mode", "critic_service_url", "service_request_timeout_seconds"}
    }
    verifier = {
        key: filtered[key]
        for key in filtered
        if key.startswith("briefing_verifier_")
        or key in {"verifier_service_url", "service_request_timeout_seconds"}
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


def _handoff_result(
    *,
    workflow_mode: str,
    decision: str,
    briefing_id: UUID | None = None,
    item_count: int | None = None,
    human_message: str = "",
) -> WriterHandoffResult:
    """Build one typed handoff result for internal workflow orchestration."""
    return WriterHandoffResult(
        handoff=WriterHandoff(
            mode=workflow_mode,
            decision=decision,
            briefing_id=briefing_id,
            item_count=item_count,
        ),
        human_message=human_message,
    )


def _ai_publish_gate_feedback(review_payload: dict[str, Any] | None) -> list[str]:
    """Convert one AI gate review payload into trace-friendly feedback strings."""
    if not isinstance(review_payload, dict):
        return []
    feedback: list[str] = []
    decision = str(review_payload.get("decision") or "").strip()
    if decision:
        feedback.append(f"AI editorial decision: {decision}")
    for tag in review_payload.get("issue_tags") or []:
        text = str(tag).strip()
        if text:
            feedback.append(f"AI issue tag: {text}")
    notes = str(review_payload.get("notes") or "").strip()
    if notes:
        feedback.append(notes)
    return feedback


def _append_ai_publish_gate_round(
    candidate: dict[str, Any],
    *,
    draft_input: str,
    gate_result: dict[str, Any],
    critique_result: dict[str, Any],
    verifier_result: dict[str, Any],
    feedback: list[str],
    rewrite_output: str | None,
    passed: bool,
) -> None:
    """Append one synthetic trace round for the pre-publish AI gate."""
    rounds = list(candidate.get("rounds") or [])
    rounds.append(
        {
            "round_index": len(rounds),
            "phase": "ai_review_gate",
            "draft_input": draft_input,
            "gate_result": gate_result,
            "critique_result": critique_result,
            "verifier_result": verifier_result,
            "feedback": feedback,
            "rewrite_output": rewrite_output,
            "passed": passed,
        }
    )
    candidate["rounds"] = rounds


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


async def _create_trace_run(
    *,
    source: str,
    mode: str,
    selected_items: list[dict[str, Any]],
    trace_metadata: Any,
    config_snapshot: dict[str, Any],
    selection_trace: dict[str, Any],
    git_sha: str | None,
    initial_draft: str | None,
) -> UUID:
    """Create one generation trace run with structured selection diagnostics."""
    return await create_generation_run(
        trigger_source=source,
        mode=mode,
        selected_item_ids=[
            item_id
            for item_id in (
                _coerce_uuid(item.get("id")) for item in selected_items
            )
            if item_id is not None
        ],
        selected_items=selected_items,
        llm_model=trace_metadata.llm_model,
        llm_model_version=trace_metadata.llm_model_version,
        prompts=trace_metadata.prompts,
        config_snapshot=config_snapshot,
        selection_trace=selection_trace,
        git_sha=git_sha,
        initial_draft=initial_draft,
    )


async def execute_generation_result(
    settings: Settings,
    *,
    mode: str,
    writer_service: WriterWorkflow | None = None,
    source: str = "workflow_service",
    manage_pool: bool = True,
) -> WriterHandoffResult:
    """Claim items, generate one briefing candidate, and return a typed outcome."""
    await get_pool(settings)
    active_service = writer_service or build_writer_workflow(settings)
    owns_service = writer_service is None
    claimed_item_ids: list[UUID] = []
    candidate_pool_rows: list[dict[str, Any]] = []
    recent_published_rows: list[dict[str, Any]] = []

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
            candidate_pool_rows = [
                dict(row)
                for row in await preview_generation_candidate_pool(
                    min_score=settings.relevance_threshold,
                    hours=settings.briefing_lookback_hours,
                    limit=claim_limit,
                )
            ]
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
            recent_published_rows = [
                dict(row)
                for row in await get_recent_published_items(
                    hours=settings.briefing_novelty_lookback_hours,
                    limit=settings.briefing_novelty_max_items,
                )
            ]

        trace_metadata = await active_service.get_trace_metadata()
        config_snapshot = _component_config_snapshot(settings)
        git_sha = _git_sha()

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
            selection_trace = (
                build_selection_trace(
                    active_service,
                    pool_items=candidate_pool_rows,
                    writer_items=[],
                    selected_items=[],
                    recent_published=recent_published_rows,
                    workflow_mode=workflow_mode,
                    decision="skip",
                    reason="no_items_in_writer_input_pool",
                    message=message,
                    selection_mode=(
                        "monthly_newsletter"
                        if workflow_mode == REGULAR_MONTHLY_NEWSLETTER_MODE
                        else "standard"
                    ),
                )
                if workflow_mode != REGULAR_MONTHLY_NEWSLETTER_MODE
                else {
                    "workflow_mode": workflow_mode,
                    "selection_mode": "monthly_newsletter",
                    "decision": "skip",
                    "reason": "no_items_in_writer_input_pool",
                    "message": message,
                    "pool_count": 0,
                    "writer_input_count": 0,
                    "selected_count": 0,
                    "pool_high_signal_count": 0,
                    "writer_high_signal_count": 0,
                    "blocked_existing_briefing_count": 0,
                    "source_floor_reject_count": 0,
                    "recent_novelty_reject_count": 0,
                    "selected_items": [],
                    "excluded_items": [],
                    "pool_items": [],
                }
            )
            trace_run_id = await _create_trace_run(
                source=source,
                mode=workflow_mode,
                selected_items=[],
                trace_metadata=trace_metadata,
                config_snapshot=config_snapshot,
                selection_trace=selection_trace,
                git_sha=git_sha,
                initial_draft=None,
            )
            await finalize_generation_run(
                run_id=trace_run_id,
                decision="BLOCKED",
                decision_reason=message,
                rewrite_count=0,
                final_draft=None,
                final_gate={},
                final_critique={},
                final_verifier={},
                briefing_id=None,
            )
            try:
                from bcn.common.alerts import consecutive_quiet_skips
                from bcn.common.alerts import quiet_streak_alert_due
                from bcn.common.alerts import send_operator_alert

                streak = await consecutive_quiet_skips()
                threshold = int(
                    getattr(settings, "alert_quiet_streak_threshold", 4) or 4
                )
                if quiet_streak_alert_due(streak, threshold):
                    await send_operator_alert(
                        settings,
                        f"{streak} consecutive quiet-day skips - a streak this "
                        "long usually means the analyst or ingestion is down, "
                        "not a quiet news cycle.",
                    )
            except Exception:
                logger.exception("Quiet-streak alert check failed")
            return _handoff_result(
                workflow_mode=workflow_mode,
                decision="skip",
                item_count=0,
                human_message=message,
            )

        item_dicts = [dict(item) for item in items]
        selection_plan = await active_service.select_items_for_workflow(
            item_dicts=item_dicts,
            workflow_mode=workflow_mode,
            recent_published=recent_published_rows,
        )
        generation_mode = str(selection_plan.get("mode") or "standard")
        selected_items = list(selection_plan.get("selected_items") or [])
        selection_reason = str(selection_plan.get("reason") or "").strip()
        selection_message = str(selection_plan.get("message") or "").strip()
        selection_trace = build_selection_trace(
            active_service,
            pool_items=candidate_pool_rows or item_dicts,
            writer_items=item_dicts,
            selected_items=selected_items,
            recent_published=recent_published_rows,
            workflow_mode=workflow_mode,
            decision=str(selection_plan.get("decision") or "skip").strip().lower(),
            reason=selection_reason,
            message=selection_message,
            selection_mode=generation_mode,
        )
        if str(selection_plan.get("decision") or "skip").strip().lower() != "generate":
            skip_message = selection_message
            if not skip_message:
                skip_message = "Selection plan skipped generation."
            logger.info(skip_message)
            trace_run_id = await _create_trace_run(
                source=source,
                mode=generation_mode,
                selected_items=selected_items,
                trace_metadata=trace_metadata,
                config_snapshot=config_snapshot,
                selection_trace=selection_trace,
                git_sha=git_sha,
                initial_draft=None,
            )
            await finalize_generation_run(
                run_id=trace_run_id,
                decision="BLOCKED",
                decision_reason=skip_message,
                rewrite_count=0,
                final_draft=None,
                final_gate={},
                final_critique={},
                final_verifier={},
                briefing_id=None,
            )
            return _handoff_result(
                workflow_mode=workflow_mode,
                decision="skip",
                item_count=len(selected_items),
                human_message=skip_message,
            )

        history_rows = await get_recent_briefings(limit=settings.briefing_history_items)
        history_items = [dict(row) for row in history_rows]
        trace_run_id: UUID | None = None
        candidate: dict[str, Any] | None = None
        final_selected_items = list(selected_items)
        ai_publish_gate_result = None
        try:
            candidate = await active_service.generate_release_candidate(
                selected_items=selected_items,
                history=history_items,
                mode=generation_mode,
            )
            final_selected_items = list(candidate.get("selected_items") or selected_items)
            selection_trace = build_selection_trace(
                active_service,
                pool_items=candidate_pool_rows or item_dicts,
                writer_items=item_dicts,
                selected_items=final_selected_items,
                recent_published=recent_published_rows,
                workflow_mode=workflow_mode,
                decision="generate",
                reason=selection_reason or "selection_ready",
                message=selection_message,
                selection_mode=generation_mode,
            )
            trace_run_id = await _create_trace_run(
                source=source,
                mode=generation_mode,
                selected_items=final_selected_items,
                trace_metadata=trace_metadata,
                config_snapshot=config_snapshot,
                selection_trace=selection_trace,
                git_sha=git_sha,
                initial_draft=(
                    str(candidate["rounds"][0]["draft_input"])
                    if candidate.get("rounds")
                    else str(candidate.get("markdown") or "")
                ),
            )
            if (
                bool(candidate.get("release_passed", False))
                and bool(settings.ai_review_publish_gate_enabled)
            ):
                original_markdown = str(candidate.get("markdown") or "")
                try:
                    ai_publish_gate_result = await run_prepublish_ai_review_gate(
                        settings,
                        content_markdown=original_markdown,
                        selected_items=final_selected_items,
                        latest_run_model=trace_metadata.llm_model,
                        latest_run_decision="PUBLISHED",
                        rewrite_count=int(candidate.get("rewrites", 0)),
                    )
                except Exception as exc:
                    logger.exception("Pre-publish AI editorial gate failed")
                    ai_gate_payload = {
                        "passed": False,
                        "action": "error",
                        "reason": (
                            "AI editorial gate failed before publish: "
                            f"{type(exc).__name__}"
                        ),
                        "decision": "error",
                        "reviewer_provider": "openai",
                        "reviewer_model": str(settings.ai_review_model or "").strip(),
                        "reasoning_effort": (
                            str(settings.ai_review_reasoning_effort or "").strip()
                            or None
                        ),
                        "issue_tags": [],
                        "notes": str(exc)[:400],
                    }
                    gate = (
                        dict(candidate.get("gate"))
                        if isinstance(candidate.get("gate"), dict)
                        else {}
                    )
                    gate["ai_review_gate"] = ai_gate_payload
                    candidate["gate"] = gate
                    candidate["release_passed"] = False
                    candidate["critic_threshold_passed"] = False
                    _append_ai_publish_gate_round(
                        candidate,
                        draft_input=original_markdown,
                        gate_result=gate,
                        critique_result=(
                            dict(candidate.get("critique"))
                            if isinstance(candidate.get("critique"), dict)
                            else {}
                        ),
                        verifier_result=(
                            dict(candidate.get("verifier"))
                            if isinstance(candidate.get("verifier"), dict)
                            else {}
                        ),
                        feedback=[ai_gate_payload["reason"], str(exc)[:400]],
                        rewrite_output=None,
                        passed=False,
                    )
                else:
                    ai_gate_payload = ai_publish_gate_result.as_gate_payload()
                    if ai_publish_gate_result.action == "rewrite":
                        rewritten_markdown = str(ai_publish_gate_result.markdown or "").strip()
                        rewritten_evaluation = await active_service.evaluate_existing_markdown(
                            markdown=rewritten_markdown,
                            selected_items=final_selected_items,
                            history=history_items,
                            mode=generation_mode,
                        )
                        gate = (
                            dict(rewritten_evaluation.get("gate"))
                            if isinstance(rewritten_evaluation.get("gate"), dict)
                            else {}
                        )
                        gate["ai_review_gate"] = ai_gate_payload
                        critique = (
                            dict(rewritten_evaluation.get("critique"))
                            if isinstance(rewritten_evaluation.get("critique"), dict)
                            else {}
                        )
                        verifier = (
                            dict(rewritten_evaluation.get("verifier"))
                            if isinstance(rewritten_evaluation.get("verifier"), dict)
                            else {}
                        )
                        candidate["markdown"] = str(
                            rewritten_evaluation.get("markdown") or rewritten_markdown
                        )
                        candidate["gate"] = gate
                        candidate["critique"] = critique
                        candidate["verifier"] = verifier
                        candidate["release_passed"] = bool(
                            rewritten_evaluation.get("release_passed", False)
                        )
                        candidate["critic_threshold_passed"] = bool(
                            rewritten_evaluation.get("critic_threshold_passed", False)
                        )
                        candidate["selected_items"] = list(
                            rewritten_evaluation.get("selected_items")
                            or final_selected_items
                        )
                        final_selected_items = list(candidate["selected_items"])
                        _append_ai_publish_gate_round(
                            candidate,
                            draft_input=original_markdown,
                            gate_result=gate,
                            critique_result=critique,
                            verifier_result=verifier,
                            feedback=_ai_publish_gate_feedback(ai_gate_payload),
                            rewrite_output=candidate["markdown"],
                            passed=bool(candidate.get("release_passed", False)),
                        )
                    else:
                        gate = (
                            dict(candidate.get("gate"))
                            if isinstance(candidate.get("gate"), dict)
                            else {}
                        )
                        gate["ai_review_gate"] = ai_gate_payload
                        candidate["gate"] = gate
                        if ai_publish_gate_result.action == "block":
                            candidate["release_passed"] = False
                            candidate["critic_threshold_passed"] = False
                        _append_ai_publish_gate_round(
                            candidate,
                            draft_input=original_markdown,
                            gate_result=gate,
                            critique_result=(
                                dict(candidate.get("critique"))
                                if isinstance(candidate.get("critique"), dict)
                                else {}
                            ),
                            verifier_result=(
                                dict(candidate.get("verifier"))
                                if isinstance(candidate.get("verifier"), dict)
                                else {}
                            ),
                            feedback=_ai_publish_gate_feedback(ai_gate_payload),
                            rewrite_output=None,
                            passed=ai_publish_gate_result.action != "block",
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
                ai_review_gate = (
                    dict(gate.get("ai_review_gate"))
                    if isinstance(gate.get("ai_review_gate"), dict)
                    else {}
                )
                if ai_review_gate and not bool(ai_review_gate.get("passed", True)):
                    message = (
                        "Blocking publish: AI editorial gate rejected the draft. "
                        f"action={str(ai_review_gate.get('action') or 'block')} "
                        f"decision={str(ai_review_gate.get('decision') or 'error')} "
                        f"reviewer={str(ai_review_gate.get('reviewer_model') or 'unknown')} "
                        f"reason={str(ai_review_gate.get('reason') or '').strip()}"
                    )
                else:
                    message = (
                        "Blocking publish: briefing did not meet release thresholds after "
                        f"{int(candidate.get('rewrites', 0))} rewrite(s). "
                        f"gate={bool(gate.get('passed', False))} "
                        f"critic={bool(candidate.get('critic_threshold_passed', False))} "
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
                return _handoff_result(
                    workflow_mode=workflow_mode,
                    decision="blocked",
                    item_count=len(final_selected_items),
                    human_message=message,
                )

            artifact = await active_service.build_release_artifact(
                briefing_body=str(candidate.get("markdown") or ""),
                selected_items=final_selected_items,
                mode=generation_mode,
            )
            item_ids = [
                item_id
                for item_id in (
                    _coerce_uuid(item.get("id")) for item in final_selected_items
                )
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
            message = f"Briefing created: id={briefing_id} items={len(final_selected_items)}"
            logger.info(message)
            if ai_publish_gate_result is not None and ai_publish_gate_result.review is not None:
                try:
                    await insert_ai_review(
                        briefing_id=briefing_id,
                        run_id=trace_run_id,
                        source="pre_publish_gate",
                        reviewer_provider=ai_publish_gate_result.review.reviewer_provider,
                        reviewer_model=ai_publish_gate_result.review.reviewer_model,
                        reasoning_effort=ai_publish_gate_result.review.reasoning_effort,
                        decision=ai_publish_gate_result.review.decision,
                        issue_tags=ai_publish_gate_result.review.issue_tags,
                        edited_markdown=ai_publish_gate_result.review.edited_markdown,
                        notes=ai_publish_gate_result.review.notes,
                        raw_response=ai_publish_gate_result.review.raw_response,
                    )
                except Exception:
                    logger.exception(
                        "Failed to persist pre-publish AI review for briefing %s",
                        briefing_id,
                    )
            return _handoff_result(
                workflow_mode=workflow_mode,
                decision="publish",
                briefing_id=briefing_id,
                item_count=len(final_selected_items),
                human_message=message,
            )
        except Exception as exc:
            logger.exception("Writer generation service failed")
            try:
                from bcn.common.alerts import send_operator_alert

                await send_operator_alert(
                    settings,
                    f"Publish workflow failed: {type(exc).__name__}: "
                    f"{str(exc)[:200]}",
                )
            except Exception:
                logger.exception("Publish-failure alert delivery failed")
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
            return _handoff_result(
                workflow_mode=workflow_mode,
                decision="blocked",
                item_count=len(final_selected_items),
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


async def execute_generation(
    settings: Settings,
    *,
    mode: str,
    writer_service: WriterWorkflow | None = None,
    source: str = "workflow_service",
    manage_pool: bool = True,
) -> str:
    """Claim items, generate one briefing candidate, and persist the outcome."""
    result = await execute_generation_result(
        settings,
        mode=mode,
        writer_service=writer_service,
        source=source,
        manage_pool=manage_pool,
    )
    return render_writer_handoff_message(
        result.handoff,
        human_message=result.human_message,
    )
