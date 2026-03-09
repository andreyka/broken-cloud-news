"""Evaluation lanes for replay, benchmark, and shadow comparisons."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
import json
from pathlib import Path
from statistics import mean
from typing import Any

from bcn.common.config import Settings
from bcn.persistence.briefings import get_distributed_briefings
from bcn.persistence.briefings import get_recent_briefings
from bcn.persistence.news_items import get_recent_published_items
from bcn.persistence.news_items import get_top_items_for_period
from bcn.persistence.news_items import preview_analyzed_items
from bcn.persistence.training import get_generation_runs_for_export
from bcn.persistence.training import get_human_reviews
from bcn.contracts.services import WriterWorkflow
from bcn.service_registry import build_writer_workflow
from bcn.workflows.modes import REGULAR_DAILY_BRIEFING_MODE
from bcn.workflows.modes import REGULAR_MONTHLY_NEWSLETTER_MODE

from .simulation import _strip_cover_image
from .simulation import score_feedback_rubric


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_json(value: Any, default: Any) -> Any:
    if isinstance(value, type(default)):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return default
        if isinstance(parsed, type(default)):
            return parsed
    return default


def _json_safe(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def load_settings_with_overrides(
    base_settings: Settings,
    overrides_path: str | None = None,
) -> tuple[Settings, dict[str, Any]]:
    """Return validated settings merged with optional JSON overrides."""
    if not overrides_path:
        return base_settings, {}

    raw = Path(overrides_path).read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Settings overrides must be a JSON object.")

    merged = dict(base_settings.model_dump())
    for key, value in payload.items():
        merged[str(key)] = value
    return Settings(**merged), {str(k): v for k, v in payload.items()}


async def _select_items_for_workflow(
    writer: WriterWorkflow,
    item_dicts: list[dict[str, Any]],
    workflow_mode: str,
    recent_published: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Select items for one workflow mode without mutating DB state."""
    return await writer.select_items_for_workflow(
        item_dicts,
        workflow_mode,
        recent_published=recent_published,
    )


async def _evaluate_existing_markdown(
    writer: WriterWorkflow,
    *,
    markdown: str,
    selected_items: list[dict[str, Any]],
    history: list[dict[str, Any]],
    mode: str,
) -> dict[str, Any]:
    """Score one existing markdown draft against current release checks."""
    evaluation = await writer.evaluate_existing_markdown(
        markdown=markdown,
        selected_items=selected_items,
        history=history,
        mode=mode,
    )
    rubric = score_feedback_rubric(
        str(evaluation["markdown"]),
        selected_items,
        evaluation["gate"],
        min_chars=int(evaluation["min_chars"]),
        hard_max_chars=int(evaluation["hard_max_chars"]),
    )
    evaluation["rubric"] = rubric
    return evaluation


async def _generate_release_candidate(
    writer: WriterWorkflow,
    *,
    selected_items: list[dict[str, Any]],
    history: list[dict[str, Any]],
    mode: str,
) -> dict[str, Any]:
    """Generate and evaluate one release candidate without publishing it."""
    evaluation = await writer.generate_release_candidate(
        selected_items=selected_items,
        history=history,
        mode=mode,
    )
    final_selected_items = list(evaluation.get("selected_items") or selected_items)
    rubric = score_feedback_rubric(
        str(evaluation["markdown"]),
        final_selected_items,
        evaluation["gate"],
        min_chars=int(evaluation["min_chars"]),
        hard_max_chars=int(evaluation["hard_max_chars"]),
    )
    evaluation["rubric"] = rubric
    return evaluation


def _benchmark_case_pass(
    *,
    expected_publishable: bool,
    reference_result: dict[str, Any],
    candidate_result: dict[str, Any],
) -> bool:
    """Return True when candidate meets benchmark expectations for one case."""
    reference_score = int(reference_result["rubric"].get("score", 0) or 0)
    candidate_score = int(candidate_result["rubric"].get("score", 0) or 0)
    reference_hard = len(reference_result["gate"].get("hard_issues", []))
    candidate_hard = len(candidate_result["gate"].get("hard_issues", []))
    score_delta = candidate_score - reference_score
    hard_delta = candidate_hard - reference_hard

    if expected_publishable:
        return (
            bool(candidate_result["release_passed"])
            and hard_delta <= 0
            and score_delta >= -1
        )

    # Non-publishable cases are informational by default. Passing means the
    # candidate either still blocks safely or clearly improves the draft.
    return (not bool(candidate_result["release_passed"])) or score_delta >= 3


def build_benchmark_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize champion/candidate benchmark performance."""
    total = len(results)
    if total == 0:
        return {
            "count": 0,
            "champion_case_pass_rate": 0.0,
            "candidate_case_pass_rate": 0.0,
            "candidate_vs_champion_case_pass_delta": 0.0,
            "champion_avg_score_delta_to_reference": 0.0,
            "candidate_avg_score_delta_to_reference": 0.0,
            "candidate_better_cases": 0,
            "champion_better_cases": 0,
            "recommendation": "hold",
            "confidence": "low",
        }

    champion_passes = 0
    candidate_passes = 0
    candidate_better_cases = 0
    champion_better_cases = 0
    champion_release = 0
    candidate_release = 0
    champion_deltas: list[int] = []
    candidate_deltas: list[int] = []

    for row in results:
        champion = row["champion"]
        candidate = row["candidate"]
        reference = row["reference"]
        champion_passes += int(bool(champion.get("case_pass")))
        candidate_passes += int(bool(candidate.get("case_pass")))
        champion_release += int(bool(champion.get("release_passed")))
        candidate_release += int(bool(candidate.get("release_passed")))

        ref_score = int(reference.get("rubric", {}).get("score", 0) or 0)
        champion_score = int(champion.get("rubric", {}).get("score", 0) or 0)
        candidate_score = int(candidate.get("rubric", {}).get("score", 0) or 0)
        champion_deltas.append(champion_score - ref_score)
        candidate_deltas.append(candidate_score - ref_score)

        if bool(candidate.get("case_pass")) and not bool(champion.get("case_pass")):
            candidate_better_cases += 1
        elif bool(champion.get("case_pass")) and not bool(candidate.get("case_pass")):
            champion_better_cases += 1
        elif candidate_score >= champion_score + 2:
            candidate_better_cases += 1
        elif champion_score >= candidate_score + 2:
            champion_better_cases += 1

    champion_case_pass_rate = champion_passes / total
    candidate_case_pass_rate = candidate_passes / total
    champion_release_rate = champion_release / total
    candidate_release_rate = candidate_release / total
    champion_avg_delta = mean(champion_deltas) if champion_deltas else 0.0
    candidate_avg_delta = mean(candidate_deltas) if candidate_deltas else 0.0

    recommendation = "hold"
    if (
        candidate_case_pass_rate >= champion_case_pass_rate
        and candidate_release_rate >= champion_release_rate
        and candidate_avg_delta >= champion_avg_delta
        and candidate_better_cases >= champion_better_cases + 2
    ):
        recommendation = "promote_candidate"
    elif (
        candidate_case_pass_rate + 0.05 < champion_case_pass_rate
        or candidate_release_rate + 0.05 < champion_release_rate
        or champion_better_cases >= candidate_better_cases + 2
    ):
        recommendation = "keep_champion"

    confidence = "low"
    if total >= 20 and abs(candidate_case_pass_rate - champion_case_pass_rate) >= 0.1:
        confidence = "high"
    elif total >= 8:
        confidence = "medium"

    return {
        "count": total,
        "champion_case_pass_rate": round(champion_case_pass_rate, 3),
        "candidate_case_pass_rate": round(candidate_case_pass_rate, 3),
        "candidate_vs_champion_case_pass_delta": round(
            candidate_case_pass_rate - champion_case_pass_rate,
            3,
        ),
        "champion_release_rate": round(champion_release_rate, 3),
        "candidate_release_rate": round(candidate_release_rate, 3),
        "champion_avg_score_delta_to_reference": round(champion_avg_delta, 2),
        "candidate_avg_score_delta_to_reference": round(candidate_avg_delta, 2),
        "candidate_better_cases": candidate_better_cases,
        "champion_better_cases": champion_better_cases,
        "recommendation": recommendation,
        "confidence": confidence,
    }


def build_shadow_summary(result: dict[str, Any]) -> dict[str, Any]:
    """Summarize one live shadow comparison."""
    champion = result.get("champion", {}) if isinstance(result, dict) else {}
    candidate = result.get("candidate", {}) if isinstance(result, dict) else {}
    if not isinstance(champion, dict):
        champion = {}
    if not isinstance(candidate, dict):
        candidate = {}

    champion_decision = str(champion.get("decision") or "")
    candidate_decision = str(candidate.get("decision") or "")
    champion_release = bool(champion.get("release_passed"))
    candidate_release = bool(candidate.get("release_passed"))
    champion_score = int(champion.get("rubric", {}).get("score", 0) or 0)
    candidate_score = int(candidate.get("rubric", {}).get("score", 0) or 0)
    champion_hard = len(champion.get("gate", {}).get("hard_issues", []))
    candidate_hard = len(candidate.get("gate", {}).get("hard_issues", []))
    champion_verifier = int(champion.get("verifier", {}).get("score", 0) or 0)
    candidate_verifier = int(candidate.get("verifier", {}).get("score", 0) or 0)
    overlap_ratio = float(result.get("selection_overlap_ratio", 0.0) or 0.0)

    recommendation = "hold"
    if candidate_decision == "generate" and champion_decision == "skip" and candidate_release:
        recommendation = "promote_candidate"
    elif champion_decision == "generate" and candidate_decision == "skip" and champion_release:
        recommendation = "keep_champion"
    elif candidate_release and not champion_release:
        recommendation = "promote_candidate"
    elif champion_release and not candidate_release:
        recommendation = "keep_champion"
    elif candidate_release and champion_release:
        if (
            candidate_score >= champion_score + 2
            and candidate_hard <= champion_hard
            and candidate_verifier >= champion_verifier
        ):
            recommendation = "promote_candidate"
        elif (
            champion_score >= candidate_score + 2
            and champion_hard <= candidate_hard
            and champion_verifier >= candidate_verifier
        ):
            recommendation = "keep_champion"

    confidence = "medium" if champion_decision == candidate_decision else "low"
    if recommendation != "hold" and abs(candidate_score - champion_score) >= 4:
        confidence = "high"

    return {
        "recommendation": recommendation,
        "confidence": confidence,
        "selection_overlap_ratio": round(overlap_ratio, 3),
        "champion_score": champion_score,
        "candidate_score": candidate_score,
        "score_delta": candidate_score - champion_score,
        "champion_release_passed": champion_release,
        "candidate_release_passed": candidate_release,
    }


def build_shadow_preference_pair(
    result: dict[str, Any],
    *,
    min_overlap: float = 0.6,
) -> dict[str, Any] | None:
    """Return a preference pair from a high-signal shadow comparison.

    Shadow comparisons are only useful as future preference data when:
    - both champion and candidate produced markdown
    - the evaluator made a directional recommendation
    - confidence is at least medium
    - item selection overlap is reasonably high
    """
    summary = result.get("summary") if isinstance(result, dict) else {}
    if not isinstance(summary, dict):
        summary = build_shadow_summary(result if isinstance(result, dict) else {})

    champion = result.get("champion") if isinstance(result, dict) else {}
    candidate = result.get("candidate") if isinstance(result, dict) else {}
    if not isinstance(champion, dict):
        champion = {}
    if not isinstance(candidate, dict):
        candidate = {}

    recommendation = str(summary.get("recommendation") or "").strip().lower()
    confidence = str(summary.get("confidence") or "").strip().lower()
    overlap_ratio = float(
        result.get(
            "selection_overlap_ratio",
            summary.get("selection_overlap_ratio", 0.0),
        )
        or 0.0
    )

    if recommendation not in {"promote_candidate", "keep_champion"}:
        return None
    if confidence not in {"medium", "high"}:
        return None
    if overlap_ratio < float(min_overlap):
        return None

    champion_markdown = str(champion.get("markdown") or "").strip()
    candidate_markdown = str(candidate.get("markdown") or "").strip()
    if not champion_markdown or not candidate_markdown:
        return None

    prefer_candidate = recommendation == "promote_candidate"
    chosen = candidate_markdown if prefer_candidate else champion_markdown
    rejected = champion_markdown if prefer_candidate else candidate_markdown
    preferred_side = "candidate" if prefer_candidate else "champion"

    return {
        "preferred_side": preferred_side,
        "recommendation": recommendation,
        "confidence": confidence,
        "selection_overlap_ratio": round(overlap_ratio, 3),
        "chosen": chosen,
        "rejected": rejected,
        "rationale": (
            "shadow_lane "
            f"preferred={preferred_side} "
            f"recommendation={recommendation} "
            f"confidence={confidence} "
            f"overlap={round(overlap_ratio, 3)} "
            f"score_delta={summary.get('score_delta', 0)}"
        ),
        "context": {
            "workflow_mode": str(result.get("workflow_mode") or ""),
            "champion_selected_items": champion.get("selected_items", []),
            "candidate_selected_items": candidate.get("selected_items", []),
            "summary": summary,
        },
    }


async def build_benchmark_pack(
    settings: Settings,
    *,
    limit: int = 50,
    since_days: int = 90,
    include_unreviewed: bool = False,
    include_nonpublishable: bool = False,
) -> dict[str, Any]:
    """Build a benchmark pack from stored traces, reviews, and published history."""
    runs = await get_generation_runs_for_export(
        limit=0,
        since_days=max(0, int(since_days)),
        include_blocked=True,
    )
    if not runs:
        return {
            "generated_at": _now_iso(),
            "count": 0,
            "cases": [],
        }

    run_ids = [row["id"] for row in runs]
    reviews = await get_human_reviews(run_ids=run_ids)
    distributed = await get_distributed_briefings(limit=0, since_days=max(0, int(since_days)))

    reviews_by_run: dict[str, list[dict[str, Any]]] = {}
    for row in reviews:
        payload = dict(row)
        run_key = str(payload.get("run_id") or "")
        if run_key:
            reviews_by_run.setdefault(run_key, []).append(payload)
    for payloads in reviews_by_run.values():
        payloads.sort(key=lambda row: row.get("created_at"), reverse=True)

    published_history = sorted(distributed, key=lambda row: row["created_at"])

    cases: list[dict[str, Any]] = []
    ordered_runs = sorted(runs, key=lambda row: row["created_at"])
    for run in ordered_runs:
        run_dict = dict(run)
        run_key = str(run_dict["id"])
        selected_items = _normalize_json(run_dict.get("selected_items"), [])
        if not selected_items:
            continue

        latest_review = (reviews_by_run.get(run_key) or [None])[0]
        if latest_review is None and not include_unreviewed:
            continue

        expected_decision = ""
        issue_tags: list[str] = []
        review_notes = ""
        edited_markdown = ""
        if latest_review is not None:
            expected_decision = str(latest_review.get("decision") or "").strip().lower()
            issue_tags = [
                str(tag).strip()
                for tag in (_normalize_json(latest_review.get("issue_tags"), []) or [])
                if str(tag).strip()
            ]
            review_notes = str(latest_review.get("notes") or "").strip()
            edited_markdown = str(latest_review.get("edited_markdown") or "").strip()
        elif str(run_dict.get("decision") or "").upper() == "PUBLISHED":
            expected_decision = "accept"
        else:
            expected_decision = "needs_work"

        expected_publishable = expected_decision in {"accept", "edit"}
        if not include_nonpublishable and not expected_publishable:
            continue

        reference_markdown = edited_markdown or str(run_dict.get("final_draft") or "").strip()
        if not reference_markdown:
            continue
        reference_markdown = _strip_cover_image(reference_markdown)

        created_at = run_dict.get("created_at")
        history_rows = [
            {
                "id": str(prev["id"]),
                "content_markdown": _strip_cover_image(
                    str(prev.get("content_markdown") or "")
                ),
            }
            for prev in published_history
            if prev["created_at"] < created_at
        ]
        history = history_rows[-int(settings.briefing_history_items) :]

        cases.append(
            {
                "case_id": run_key,
                "run_id": run_key,
                "briefing_id": (
                    str(run_dict.get("briefing_id")) if run_dict.get("briefing_id") else None
                ),
                "created_at": _json_safe(created_at),
                "mode": str(run_dict.get("mode") or "standard"),
                "expected_decision": expected_decision,
                "expected_publishable": expected_publishable,
                "issue_tags": issue_tags,
                "notes": review_notes,
                "selected_items": selected_items,
                "history": history,
                "reference_markdown": reference_markdown,
            }
        )

    if limit > 0:
        cases = cases[-max(1, int(limit)) :]

    return {
        "generated_at": _now_iso(),
        "count": len(cases),
        "source": {
            "since_days": max(0, int(since_days)),
            "include_unreviewed": bool(include_unreviewed),
            "include_nonpublishable": bool(include_nonpublishable),
        },
        "cases": cases,
    }


async def run_benchmark_pack(
    settings: Settings,
    *,
    cases_path: str,
    candidate_overrides_path: str | None = None,
    include_text: bool = False,
) -> dict[str, Any]:
    """Run champion and candidate against a curated benchmark pack."""
    pack = json.loads(Path(cases_path).read_text(encoding="utf-8"))
    cases = pack.get("cases", []) if isinstance(pack, dict) else []
    if not isinstance(cases, list):
        raise ValueError("Benchmark pack must contain a 'cases' list.")

    candidate_settings, candidate_overrides = load_settings_with_overrides(
        settings,
        candidate_overrides_path,
    )
    champion_writer = build_writer_workflow(settings)
    candidate_writer = champion_writer
    if candidate_settings.model_dump() != settings.model_dump():
        candidate_writer = build_writer_workflow(candidate_settings)

    try:
        results: list[dict[str, Any]] = []
        for raw_case in cases:
            if not isinstance(raw_case, dict):
                continue
            selected_items = [
                dict(item)
                for item in (_normalize_json(raw_case.get("selected_items"), []) or [])
                if isinstance(item, dict)
            ]
            if not selected_items:
                continue
            history = [
                {
                    "id": str(entry.get("id") or ""),
                    "content_markdown": str(entry.get("content_markdown") or ""),
                }
                for entry in (_normalize_json(raw_case.get("history"), []) or [])
                if isinstance(entry, dict)
            ]
            mode = str(raw_case.get("mode") or "standard")
            reference_markdown = _strip_cover_image(
                str(raw_case.get("reference_markdown") or "")
            )
            expected_publishable = bool(raw_case.get("expected_publishable", True))

            reference_result = await _evaluate_existing_markdown(
                champion_writer,
                markdown=reference_markdown,
                selected_items=selected_items,
                history=history,
                mode=mode,
            )
            champion_result = await _generate_release_candidate(
                champion_writer,
                selected_items=selected_items,
                history=history,
                mode=mode,
            )
            candidate_result = champion_result
            if candidate_writer is not champion_writer:
                candidate_result = await _generate_release_candidate(
                    candidate_writer,
                    selected_items=selected_items,
                    history=history,
                    mode=mode,
                )

            champion_result = dict(champion_result)
            candidate_result = dict(candidate_result)
            champion_result["case_pass"] = _benchmark_case_pass(
                expected_publishable=expected_publishable,
                reference_result=reference_result,
                candidate_result=champion_result,
            )
            candidate_result["case_pass"] = _benchmark_case_pass(
                expected_publishable=expected_publishable,
                reference_result=reference_result,
                candidate_result=candidate_result,
            )

            row: dict[str, Any] = {
                "case_id": str(raw_case.get("case_id") or ""),
                "expected_decision": str(raw_case.get("expected_decision") or ""),
                "expected_publishable": expected_publishable,
                "issue_tags": _normalize_json(raw_case.get("issue_tags"), []),
                "champion": champion_result,
                "candidate": candidate_result,
                "reference": reference_result,
            }
            if include_text:
                row["selected_items"] = selected_items
                row["history"] = history
            results.append(_json_safe(row))

        summary = build_benchmark_summary(results)
        return {
            "generated_at": _now_iso(),
            "lane": "benchmark",
            "pack_path": str(Path(cases_path).resolve()),
            "count": len(results),
            "candidate_overrides": candidate_overrides,
            "summary": summary,
            "results": results,
        }
    finally:
        await champion_writer.close()
        if candidate_writer is not champion_writer:
            await candidate_writer.close()


async def run_shadow_lane(
    settings: Settings,
    *,
    workflow_mode: str = REGULAR_DAILY_BRIEFING_MODE,
    candidate_overrides_path: str | None = None,
    include_text: bool = False,
) -> dict[str, Any]:
    """Compare champion and candidate on current upcoming items without publishing."""
    candidate_settings, candidate_overrides = load_settings_with_overrides(
        settings,
        candidate_overrides_path,
    )
    champion_writer = build_writer_workflow(settings)
    candidate_writer = champion_writer
    if candidate_settings.model_dump() != settings.model_dump():
        candidate_writer = build_writer_workflow(candidate_settings)

    try:
        if workflow_mode == REGULAR_MONTHLY_NEWSLETTER_MODE:
            item_rows = await get_top_items_for_period(
                days=max(1, int(settings.monthly_newsletter_lookback_days)),
                min_score=max(1, int(settings.monthly_newsletter_min_score)),
                limit=max(
                    int(settings.monthly_newsletter_max_items) * 4,
                    int(settings.monthly_newsletter_min_items),
                ),
            )
        else:
            claim_limit = max(int(settings.briefing_max_items) * 8, 40)
            item_rows = await preview_analyzed_items(
                min_score=settings.relevance_threshold,
                hours=settings.briefing_lookback_hours,
                limit=claim_limit,
            )
        item_dicts = [dict(row) for row in item_rows]

        history_limit = max(
            int(settings.briefing_history_items),
            int(candidate_settings.briefing_history_items),
        )
        history_rows = await get_recent_briefings(limit=history_limit)
        history = [dict(row) for row in history_rows]
        recent_published_rows = []
        if workflow_mode != REGULAR_MONTHLY_NEWSLETTER_MODE:
            recent_published_limit = max(
                int(settings.briefing_novelty_max_items),
                int(candidate_settings.briefing_novelty_max_items),
            )
            recent_published_hours = max(
                int(settings.briefing_novelty_lookback_hours),
                int(candidate_settings.briefing_novelty_lookback_hours),
            )
            recent_published_rows = await get_recent_published_items(
                hours=recent_published_hours,
                limit=recent_published_limit,
            )
        recent_published = [dict(row) for row in recent_published_rows]

        champion_plan = await _select_items_for_workflow(
            champion_writer,
            item_dicts,
            workflow_mode,
            recent_published=recent_published,
        )
        candidate_plan = await _select_items_for_workflow(
            candidate_writer,
            item_dicts,
            workflow_mode,
            recent_published=recent_published,
        )

        champion_result: dict[str, Any] = {
            "decision": str(champion_plan.get("decision") or "skip"),
            "reason": str(champion_plan.get("reason") or ""),
            "mode": str(champion_plan.get("mode") or "standard"),
            "selected_items": champion_plan.get("selected_items", []),
        }
        candidate_result: dict[str, Any] = {
            "decision": str(candidate_plan.get("decision") or "skip"),
            "reason": str(candidate_plan.get("reason") or ""),
            "mode": str(candidate_plan.get("mode") or "standard"),
            "selected_items": candidate_plan.get("selected_items", []),
        }

        if champion_result["decision"] == "generate":
            champion_result.update(
                await _generate_release_candidate(
                    champion_writer,
                    selected_items=list(champion_result["selected_items"]),
                    history=history[: int(settings.briefing_history_items)],
                    mode=str(champion_result["mode"]),
                )
            )
        if candidate_result["decision"] == "generate":
            candidate_result.update(
                await _generate_release_candidate(
                    candidate_writer,
                    selected_items=list(candidate_result["selected_items"]),
                    history=history[: int(candidate_settings.briefing_history_items)],
                    mode=str(candidate_result["mode"]),
                )
            )

        champion_ids = {
            str(item.get("id"))
            for item in list(champion_result.get("selected_items") or [])
            if item.get("id")
        }
        candidate_ids = {
            str(item.get("id"))
            for item in list(candidate_result.get("selected_items") or [])
            if item.get("id")
        }
        union = champion_ids | candidate_ids
        overlap_ratio = (len(champion_ids & candidate_ids) / len(union)) if union else 1.0

        report: dict[str, Any] = {
            "generated_at": _now_iso(),
            "lane": "shadow",
            "workflow_mode": workflow_mode,
            "candidate_overrides": candidate_overrides,
            "item_pool_count": len(item_dicts),
            "selection_overlap_ratio": round(overlap_ratio, 3),
            "champion": _json_safe(champion_result),
            "candidate": _json_safe(candidate_result),
        }
        report["summary"] = build_shadow_summary(report)
        if not include_text:
            for key in ("champion", "candidate"):
                payload = report.get(key)
                if not isinstance(payload, dict):
                    continue
                payload.pop("markdown", None)
                selected_items = payload.get("selected_items")
                if isinstance(selected_items, list):
                    payload["selected_items"] = [
                        {
                            "id": item.get("id"),
                            "title": item.get("title"),
                            "url": item.get("url"),
                            "source_type": item.get("source_type"),
                            "relevance_score": item.get("relevance_score"),
                        }
                        for item in selected_items
                        if isinstance(item, dict)
                    ]
        return report
    finally:
        await champion_writer.close()
        if candidate_writer is not champion_writer:
            await candidate_writer.close()
