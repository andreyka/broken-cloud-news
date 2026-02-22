"""Historical briefing simulation and comparison utilities.

This module replays historical distributed briefings, regenerates simulated
drafts from the same item sets, and compares actual vs simulated quality
against a feedback-aligned rubric.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from statistics import mean
import logging
import re

from bcn.agents.writer import WriterExecutor
from bcn.config import Settings
from bcn.db import get_distributed_briefings, get_items_by_ids

logger = logging.getLogger(__name__)

_AI_STAMP_PATTERNS = (
    re.compile(r"clouds?\s+are\s+getting.+tools?\s+are\s+just\s+getting", re.IGNORECASE),
    re.compile(r"\b(in\s+today'?s\s+(?:fast|rapidly)\s+evolving)\b", re.IGNORECASE),
    re.compile(r"\bever[-\s]evolving\b", re.IGNORECASE),
)

_CLOUD_TECH_TERMS = {
    "kubernetes", "k8s", "qemu", "kvm", "envoy", "postgres",
    "clickhouse", "redis", "cloudflare", "terraform", "load balancer",
    "managed database", "iam", "serverless", "quic", "container",
}

_ACTIONABLE_TERMS = {
    "cve-", "patch", "mitigation", "detect", "detection", "ioc",
    "playbook", "upgrade", "rotate", "enforce", "validate",
    "incident", "exploit", "harden",
}


def _strip_cover_image(markdown: str) -> str:
    return re.sub(r"^!\[[^\]]*]\([^)]+\)\s*\n*", "", (markdown or "").strip())


def _order_items_by_ids(items: list[dict], item_ids: list) -> list[dict]:
    pos = {str(i): idx for idx, i in enumerate(item_ids)}
    return sorted(items, key=lambda row: pos.get(str(row.get("id")), 10**6))


def _keyword_hits(text: str, keywords: set[str]) -> set[str]:
    lower = text.lower()
    return {kw for kw in keywords if kw in lower}


def _dominant_ratio(items: list[dict]) -> float:
    if not items:
        return 0.0
    counts = Counter(str(i.get("source_type", "")).lower() for i in items)
    if not counts:
        return 0.0
    return counts.most_common(1)[0][1] / max(1, len(items))


def score_feedback_rubric(
    markdown: str,
    items: list[dict],
    gate: dict[str, object],
    *,
    min_chars: int,
    hard_max_chars: int,
) -> dict[str, object]:
    """Score briefing quality using deterministic feedback-aligned heuristics."""
    body = (markdown or "").strip()
    length = len(body)
    notes: list[str] = []

    source_counts = Counter(str(i.get("source_type", "")).lower() for i in items)
    has_reddit = source_counts.get("reddit", 0) > 0
    has_cloudflare = any(
        "cloudflare" in (str(i.get("url", "")) + " " + str(i.get("title", ""))).lower()
        for i in items
    )
    dominant_ratio = _dominant_ratio(items)

    # 1) Length/depth (15)
    depth_score = 15
    if length < min_chars:
        depth_score = max(0, 15 - int((min_chars - length) / max(1, min_chars) * 15))
        notes.append(f"Too short ({length} chars vs min {min_chars}).")
    elif length > hard_max_chars:
        depth_score = max(0, 15 - int((length - hard_max_chars) / max(1, hard_max_chars) * 15))
        notes.append(f"Too long ({length} chars vs hard max {hard_max_chars}).")

    # 2) Link hygiene (20)
    hard_issues = [str(i) for i in gate.get("hard_issues", [])]
    link_hard = [i for i in hard_issues if "url" in i.lower()]
    link_score = max(0, 20 - (8 * len(link_hard)))
    if link_hard:
        notes.append("Link hygiene issues detected (missing/duplicate selected URLs).")

    # 3) Source diversity (20)
    unique_sources = len(source_counts)
    source_score = min(16, unique_sources * 6)
    if dominant_ratio >= 0.75:
        source_score -= 4
        notes.append("Single source dominates the briefing.")
    if has_reddit:
        source_score += 2
    else:
        notes.append("No Reddit-sourced item in this briefing.")
    if has_cloudflare:
        source_score += 2
    else:
        notes.append("No Cloudflare-side signal in this briefing.")
    source_score = max(0, min(20, source_score))

    # 4) Cloud-tech focus breadth (20)
    tech_text = " ".join(
        [body] + [str(i.get("title", "")) + " " + str(i.get("summary", "")) for i in items]
    )
    tech_hits = _keyword_hits(tech_text, _CLOUD_TECH_TERMS)
    cloud_focus_score = min(20, 6 + (2 * len(tech_hits)))
    if len(tech_hits) < 2:
        notes.append("Weak cloud technology breadth (few cloud-native topics).")

    # 5) Actionability (15)
    action_hits = _keyword_hits(body, _ACTIONABLE_TERMS)
    actionability_score = min(15, 3 + (2 * len(action_hits)))
    if len(action_hits) < 3:
        notes.append("Low actionability density (few patch/detect/contain cues).")

    # 6) Writing quality / anti-template style (10)
    writing_score = 10
    if any(p.search(body) for p in _AI_STAMP_PATTERNS):
        writing_score -= 3
        notes.append("Contains AI-stamp phrasing.")
    if re.search(r"(?im)^\*\*(?:detection|source|threat|response|mitigation|intel)\s*:", body):
        writing_score -= 3
        notes.append("Template-style section labels detected.")
    if re.search(r"(?im)^\*?\s*source\s*:", body):
        writing_score -= 2
        notes.append("Standalone source fields detected.")
    soft_issues = [str(i) for i in gate.get("soft_issues", [])]
    if any("repetitive" in i.lower() for i in soft_issues):
        writing_score -= 2
        notes.append("Repetitive style heuristics triggered.")
    writing_score = max(0, writing_score)

    breakdown = {
        "depth": depth_score,
        "link_hygiene": link_score,
        "source_diversity": source_score,
        "cloud_focus": cloud_focus_score,
        "actionability": actionability_score,
        "writing_quality": writing_score,
    }
    total_score = int(sum(breakdown.values()))

    return {
        "score": total_score,
        "breakdown": breakdown,
        "notes": notes[:12],
        "has_reddit": has_reddit,
        "has_cloudflare": has_cloudflare,
        "dominant_source_ratio": round(dominant_ratio, 3),
        "cloud_terms_hit": sorted(tech_hits),
    }


async def _simulate_briefing_body(
    writer: WriterExecutor,
    items: list[dict],
    recent_briefings: list[dict],
    *,
    apply_critic_rewrites: bool,
) -> tuple[str, dict[str, object]]:
    quiet_mode = writer._is_quiet_day(items)
    mode = "quiet_day" if quiet_mode else "standard"
    min_chars, target_chars, hard_max_chars = writer._char_limits(
        mode,
        selected_count=len(items),
    )

    briefing_body = await writer.llm.generate_briefing(
        items,
        recent_briefings=recent_briefings,
        mode=mode,
    )
    briefing_body = await writer._postprocess_briefing(
        briefing_body=briefing_body,
        selected_items=items,
        mode=mode,
        min_chars=min_chars,
        target_chars=target_chars,
        hard_max_chars=hard_max_chars,
    )

    rewrites = 0
    if apply_critic_rewrites and writer.settings.briefing_critique_enabled:
        max_rewrites = max(0, int(writer.settings.briefing_critique_max_rounds))
        while True:
            gate = writer._quality_gate(
                markdown=briefing_body,
                selected_items=items,
                mode=mode,
                min_chars=min_chars,
                hard_max_chars=hard_max_chars,
            )
            critique = await writer.llm.critique_briefing(
                draft_markdown=briefing_body,
                items=items,
                mode=mode,
                gate_hard_issues=[str(i) for i in gate.get("hard_issues", [])],
                gate_soft_issues=[str(i) for i in gate.get("soft_issues", [])],
            )
            gate_passed = bool(gate.get("passed", False))
            critic_passed = bool(critique.get("passed", False))
            if gate_passed and critic_passed:
                break
            if rewrites >= max_rewrites:
                break

            feedback: list[str] = []
            feedback.extend(gate.get("issues", []))
            feedback.extend([str(i) for i in critique.get("issues", [])])
            feedback.extend([str(r) for r in critique.get("recommendations", [])])

            rewrites += 1
            briefing_body = await writer.llm.revise_briefing(
                draft_markdown=briefing_body,
                items=items,
                feedback=feedback,
                mode=mode,
                min_chars=min_chars,
                target_chars=target_chars,
                hard_max_chars=hard_max_chars,
            )
            briefing_body = await writer._postprocess_briefing(
                briefing_body=briefing_body,
                selected_items=items,
                mode=mode,
                min_chars=min_chars,
                target_chars=target_chars,
                hard_max_chars=hard_max_chars,
            )

    briefing_body = writer._normalize_section_headings(briefing_body)
    briefing_body = writer._de_template_fields(briefing_body)

    meta = {
        "mode": mode,
        "rewrites": rewrites,
        "min_chars": min_chars,
        "hard_max_chars": hard_max_chars,
    }
    return briefing_body, meta


async def simulate_historical_briefings(
    settings: Settings,
    *,
    limit: int = 0,
    since_days: int = 0,
    include_text: bool = False,
    apply_critic_rewrites: bool = False,
) -> dict[str, object]:
    """Replay historical distributed briefings and compare simulated output."""
    briefings = await get_distributed_briefings(limit=limit, since_days=since_days)
    if not briefings:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "count": 0,
            "results": [],
            "summary": {
                "avg_actual_score": 0.0,
                "avg_simulated_score": 0.0,
                "avg_delta": 0.0,
                "improved": 0,
                "regressed": 0,
                "equal": 0,
            },
        }

    # Replay oldest -> newest for realistic style memory context.
    ordered = sorted(briefings, key=lambda b: b["created_at"])
    writer = WriterExecutor(settings)
    results: list[dict[str, object]] = []
    recurring_notes: Counter[str] = Counter()

    for idx, briefing in enumerate(ordered):
        item_ids = list(briefing.get("item_ids") or [])
        if not item_ids:
            continue
        item_rows = await get_items_by_ids(item_ids)
        items = _order_items_by_ids([dict(r) for r in item_rows], item_ids)
        if not items:
            continue

        history_start = max(0, idx - int(settings.briefing_history_items))
        history = [
            {
                "id": str(prev["id"]),
                "content_markdown": _strip_cover_image(str(prev.get("content_markdown") or "")),
            }
            for prev in ordered[history_start:idx]
        ]

        simulated_body, meta = await _simulate_briefing_body(
            writer,
            items,
            history,
            apply_critic_rewrites=apply_critic_rewrites,
        )
        actual_body = _strip_cover_image(str(briefing.get("content_markdown") or ""))

        mode = str(meta["mode"])
        min_chars = int(meta["min_chars"])
        hard_max_chars = int(meta["hard_max_chars"])

        actual_gate = writer._quality_gate(
            markdown=actual_body,
            selected_items=items,
            mode=mode,
            min_chars=min_chars,
            hard_max_chars=hard_max_chars,
        )
        simulated_gate = writer._quality_gate(
            markdown=simulated_body,
            selected_items=items,
            mode=mode,
            min_chars=min_chars,
            hard_max_chars=hard_max_chars,
        )

        actual_eval = score_feedback_rubric(
            actual_body,
            items,
            actual_gate,
            min_chars=min_chars,
            hard_max_chars=hard_max_chars,
        )
        simulated_eval = score_feedback_rubric(
            simulated_body,
            items,
            simulated_gate,
            min_chars=min_chars,
            hard_max_chars=hard_max_chars,
        )

        for note in actual_eval["notes"]:
            recurring_notes[str(note)] += 1

        delta = int(simulated_eval["score"]) - int(actual_eval["score"])
        entry: dict[str, object] = {
            "briefing_id": str(briefing["id"]),
            "created_at": briefing["created_at"].isoformat(),
            "item_count": len(items),
            "mode": mode,
            "simulated_rewrites": int(meta["rewrites"]),
            "actual_score": int(actual_eval["score"]),
            "simulated_score": int(simulated_eval["score"]),
            "delta": delta,
            "actual_breakdown": actual_eval["breakdown"],
            "simulated_breakdown": simulated_eval["breakdown"],
            "actual_notes": actual_eval["notes"],
            "simulated_notes": simulated_eval["notes"],
            "actual_gate_hard_issues": [str(i) for i in actual_gate.get("hard_issues", [])],
            "simulated_gate_hard_issues": [str(i) for i in simulated_gate.get("hard_issues", [])],
        }
        if include_text:
            entry["actual_markdown"] = actual_body
            entry["simulated_markdown"] = simulated_body
        results.append(entry)
        if (idx + 1) % 5 == 0:
            logger.info(
                "Simulation progress: %d/%d briefings processed",
                idx + 1,
                len(ordered),
            )

    actual_scores = [int(r["actual_score"]) for r in results]
    simulated_scores = [int(r["simulated_score"]) for r in results]
    deltas = [int(r["delta"]) for r in results]

    summary = {
        "avg_actual_score": round(mean(actual_scores), 2) if actual_scores else 0.0,
        "avg_simulated_score": round(mean(simulated_scores), 2) if simulated_scores else 0.0,
        "avg_delta": round(mean(deltas), 2) if deltas else 0.0,
        "improved": sum(1 for d in deltas if d > 0),
        "regressed": sum(1 for d in deltas if d < 0),
        "equal": sum(1 for d in deltas if d == 0),
        "top_actual_feedback_gaps": [
            {"issue": issue, "count": count}
            for issue, count in recurring_notes.most_common(8)
        ],
    }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(results),
        "limit": int(limit),
        "since_days": int(since_days),
        "apply_critic_rewrites": bool(apply_critic_rewrites),
        "results": results,
        "summary": summary,
    }


def compare_simulation_reports(
    current_report: dict[str, object],
    previous_report: dict[str, object],
) -> dict[str, object]:
    """Compare two simulation reports and return run-over-run quality deltas."""

    def _result_map(report: dict[str, object]) -> dict[str, dict[str, object]]:
        mapped: dict[str, dict[str, object]] = {}
        rows = report.get("results")
        if not isinstance(rows, list):
            return mapped
        for row in rows:
            if not isinstance(row, dict):
                continue
            briefing_id = str(row.get("briefing_id") or "").strip()
            if not briefing_id:
                continue
            mapped[briefing_id] = row
        return mapped

    current_summary = current_report.get("summary")
    if not isinstance(current_summary, dict):
        current_summary = {}
    previous_summary = previous_report.get("summary")
    if not isinstance(previous_summary, dict):
        previous_summary = {}

    current_results = _result_map(current_report)
    previous_results = _result_map(previous_report)
    current_ids = set(current_results.keys())
    previous_ids = set(previous_results.keys())
    overlap = sorted(current_ids & previous_ids)

    score_changes: list[tuple[str, int, int, int]] = []
    for briefing_id in overlap:
        current_score = int(current_results[briefing_id].get("simulated_score", 0))
        previous_score = int(previous_results[briefing_id].get("simulated_score", 0))
        score_changes.append((briefing_id, current_score - previous_score, current_score, previous_score))

    gains = [row for row in score_changes if row[1] > 0]
    losses = [row for row in score_changes if row[1] < 0]
    equals = [row for row in score_changes if row[1] == 0]
    score_delta_values = [row[1] for row in score_changes]

    top_gains = sorted(gains, key=lambda row: row[1], reverse=True)[:5]
    top_losses = sorted(losses, key=lambda row: row[1])[:5]

    def _fmt(rows: list[tuple[str, int, int, int]]) -> list[dict[str, object]]:
        return [
            {
                "briefing_id": briefing_id,
                "score_change": change,
                "current_simulated_score": current_score,
                "previous_simulated_score": previous_score,
            }
            for briefing_id, change, current_score, previous_score in rows
        ]

    return {
        "baseline_generated_at": previous_report.get("generated_at"),
        "current_generated_at": current_report.get("generated_at"),
        "baseline_count": int(previous_report.get("count", 0) or 0),
        "current_count": int(current_report.get("count", 0) or 0),
        "overlap_count": len(overlap),
        "new_briefings_in_current": len(current_ids - previous_ids),
        "missing_briefings_from_previous": len(previous_ids - current_ids),
        "avg_simulated_score_change": round(mean(score_delta_values), 2) if score_delta_values else 0.0,
        "avg_delta_change": round(
            float(current_summary.get("avg_delta", 0) or 0) - float(previous_summary.get("avg_delta", 0) or 0),
            2,
        ),
        "improved_vs_previous": len(gains),
        "regressed_vs_previous": len(losses),
        "unchanged_vs_previous": len(equals),
        "top_gains": _fmt(top_gains),
        "top_losses": _fmt(top_losses),
    }
