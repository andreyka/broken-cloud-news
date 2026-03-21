"""Historical briefing simulation and comparison utilities.

This module replays historical distributed briefings, regenerates simulated
drafts from the same item sets, and compares actual vs simulated quality
against a feedback-aligned rubric.
"""

from __future__ import annotations

from collections.abc import Awaitable
from collections.abc import Callable
from collections import Counter
from datetime import datetime
from datetime import timezone
import logging
from math import comb
import re
from statistics import mean
from statistics import pstdev

from bcn.services.analyst.service import AnalystService
from bcn.services.analyst.service import AnalystWorkflowProtocol
from bcn.common.config import Settings
from bcn.evaluation.overrides import load_settings_with_overrides
from bcn.persistence.briefings import get_distributed_briefings
from bcn.persistence.news_items import get_items_by_ids
from bcn.persistence.news_items import update_item_analyzed
from bcn.contracts.services import WriterWorkflow
from bcn.service_registry import build_writer_workflow

logger = logging.getLogger(__name__)
SimulationProgressCallback = Callable[[int, dict[str, object]], Awaitable[None]]

_AI_STAMP_PATTERNS = (
    re.compile(
        r"clouds?\s+are\s+getting.+tools?\s+are\s+just\s+getting", re.IGNORECASE
    ),
    re.compile(r"\b(in\s+today'?s\s+(?:fast|rapidly)\s+evolving)\b", re.IGNORECASE),
    re.compile(r"\bever[-\s]evolving\b", re.IGNORECASE),
)

_RUBRIC_DIMENSIONS = (
    "depth",
    "link_hygiene",
    "source_diversity",
    "cloud_focus",
    "actionability",
    "writing_quality",
    "novelty",
)

_CLOUD_TECH_TERMS = {
    "kubernetes",
    "k8s",
    "qemu",
    "kvm",
    "envoy",
    "postgres",
    "clickhouse",
    "redis",
    "cloudflare",
    "terraform",
    "load balancer",
    "managed database",
    "iam",
    "serverless",
    "quic",
    "container",
    "llm",
    "mlops",
    "gpu",
    "inference",
    "rag",
    "machine learning",
    "ai",
}

_ACTIONABLE_TERMS = {
    "cve-",
    "patch",
    "mitigation",
    "detect",
    "detection",
    "ioc",
    "playbook",
    "upgrade",
    "rotate",
    "enforce",
    "validate",
    "incident",
    "exploit",
    "harden",
    "supply chain",
    "zero-day",
    "protocol",
    "ddos",
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


def _rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def _percentile(values: list[int], q: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    pos = (len(values) - 1) * max(0.0, min(1.0, q))
    low = int(pos)
    high = min(len(values) - 1, low + 1)
    if low == high:
        return float(values[low])
    weight = pos - low
    return float(values[low] + (values[high] - values[low]) * weight)


def _sign_test_two_sided_p(wins: int, losses: int) -> float:
    """Exact two-sided sign-test p-value under p=0.5."""
    n = max(0, wins) + max(0, losses)
    if n == 0:
        return 1.0
    k = min(max(0, wins), max(0, losses))
    tail = sum(comb(n, i) for i in range(k + 1))
    p_value = min(1.0, 2.0 * (tail / (2**n)))
    return round(p_value, 6)


def _contains_any(lines: list[str], needles: tuple[str, ...]) -> bool:
    lowered = " | ".join(str(line).lower() for line in lines)
    return any(needle in lowered for needle in needles)


def _has_duplicate_link_issue(hard_issues: list[str]) -> bool:
    return _contains_any(
        hard_issues, ("appears multiple times", "duplicate selected url")
    )


def _is_human_writer_like(breakdown: dict[str, object], notes: list[str]) -> bool:
    writing_quality = int(breakdown.get("writing_quality", 0) or 0)
    actionability = int(breakdown.get("actionability", 0) or 0)
    depth = int(breakdown.get("depth", 0) or 0)
    style_flags = (
        "ai-stamp phrasing",
        "template-style section labels",
        "standalone source fields",
        "repetitive style",
    )
    return (
        writing_quality >= 8
        and actionability >= 10
        and depth >= 10
        and not _contains_any(notes, style_flags)
    )


def _is_formatting_clean(hard_issues: list[str], breakdown: dict[str, object]) -> bool:
    formatting_flags = (
        "missing selected url",
        "appears multiple times",
        "truncation artifact",
        "fallback phrase artifact",
        "digest too long",
        "digest too short",
    )
    link_hygiene = int(breakdown.get("link_hygiene", 0) or 0)
    return (not _contains_any(hard_issues, formatting_flags)) and link_hygiene >= 20


def _has_duplicate_story_signal(notes: list[str]) -> bool:
    return _contains_any(notes, ("repetitive style", "single source dominates"))


def _build_decision_summary(results: list[dict[str, object]]) -> dict[str, object]:
    total = len(results)
    if total == 0:
        return {
            "delta_distribution": {
                "median_delta": 0.0,
                "p10_delta": 0.0,
                "p90_delta": 0.0,
                "std_delta": 0.0,
            },
            "win_loss": {
                "wins": 0,
                "losses": 0,
                "ties": 0,
                "win_rate_no_ties": 0.0,
                "non_regression_rate": 0.0,
                "sign_test_p_value": 1.0,
            },
            "gate_quality": {
                "actual_hard_pass_rate": 0.0,
                "simulated_hard_pass_rate": 0.0,
                "hard_pass_rate_change": 0.0,
                "actual_avg_hard_issues": 0.0,
                "simulated_avg_hard_issues": 0.0,
                "avg_hard_issue_change": 0.0,
                "actual_missing_url_hard_total": 0,
                "simulated_missing_url_hard_total": 0,
            },
            "focus_metrics": {
                "human_writer_pass_rate_actual": 0.0,
                "human_writer_pass_rate_simulated": 0.0,
                "human_writer_pass_rate_change": 0.0,
                "formatting_clean_pass_rate_actual": 0.0,
                "formatting_clean_pass_rate_simulated": 0.0,
                "formatting_clean_pass_rate_change": 0.0,
                "duplicate_link_issue_rate_actual": 0.0,
                "duplicate_link_issue_rate_simulated": 0.0,
                "duplicate_link_issue_rate_change": 0.0,
                "duplicate_story_signal_rate_actual": 0.0,
                "duplicate_story_signal_rate_simulated": 0.0,
                "duplicate_story_signal_rate_change": 0.0,
            },
            "dimension_avg_delta": {dim: 0.0 for dim in _RUBRIC_DIMENSIONS},
            "decision": {
                "recommendation": "hold",
                "confidence": "low",
                "rationale": "No simulation rows available.",
            },
        }

    deltas = [int(row.get("delta", 0) or 0) for row in results]
    wins = sum(1 for d in deltas if d > 0)
    losses = sum(1 for d in deltas if d < 0)
    ties = total - wins - losses
    avg_delta = mean(deltas) if deltas else 0.0
    sorted_deltas = sorted(deltas)

    actual_hard_counts: list[int] = []
    simulated_hard_counts: list[int] = []
    actual_missing_url_hard_total = 0
    simulated_missing_url_hard_total = 0
    actual_duplicate_url_hard_total = 0
    simulated_duplicate_url_hard_total = 0
    dimension_deltas: dict[str, list[int]] = {dim: [] for dim in _RUBRIC_DIMENSIONS}
    actual_human_writer_passes = 0
    simulated_human_writer_passes = 0
    actual_formatting_clean_passes = 0
    simulated_formatting_clean_passes = 0
    actual_duplicate_story_signals = 0
    simulated_duplicate_story_signals = 0

    for row in results:
        actual_hard = [str(i) for i in (row.get("actual_gate_hard_issues") or [])]
        simulated_hard = [str(i) for i in (row.get("simulated_gate_hard_issues") or [])]
        actual_hard_counts.append(len(actual_hard))
        simulated_hard_counts.append(len(simulated_hard))
        actual_missing_url_hard_total += sum(
            1 for issue in actual_hard if "missing selected url" in issue.lower()
        )
        simulated_missing_url_hard_total += sum(
            1 for issue in simulated_hard if "missing selected url" in issue.lower()
        )
        actual_duplicate_url_hard_total += sum(
            1 for issue in actual_hard if _has_duplicate_link_issue([issue])
        )
        simulated_duplicate_url_hard_total += sum(
            1 for issue in simulated_hard if _has_duplicate_link_issue([issue])
        )

        actual_breakdown = row.get("actual_breakdown") or {}
        simulated_breakdown = row.get("simulated_breakdown") or {}
        if not isinstance(actual_breakdown, dict):
            actual_breakdown = {}
        if not isinstance(simulated_breakdown, dict):
            simulated_breakdown = {}
        actual_notes = [str(i) for i in (row.get("actual_notes") or [])]
        simulated_notes = [str(i) for i in (row.get("simulated_notes") or [])]

        if _is_human_writer_like(actual_breakdown, actual_notes):
            actual_human_writer_passes += 1
        if _is_human_writer_like(simulated_breakdown, simulated_notes):
            simulated_human_writer_passes += 1
        if _is_formatting_clean(actual_hard, actual_breakdown):
            actual_formatting_clean_passes += 1
        if _is_formatting_clean(simulated_hard, simulated_breakdown):
            simulated_formatting_clean_passes += 1
        if _has_duplicate_story_signal(actual_notes):
            actual_duplicate_story_signals += 1
        if _has_duplicate_story_signal(simulated_notes):
            simulated_duplicate_story_signals += 1

        for dim in _RUBRIC_DIMENSIONS:
            actual_val = int(actual_breakdown.get(dim, 0) or 0)
            simulated_val = int(simulated_breakdown.get(dim, 0) or 0)
            dimension_deltas[dim].append(simulated_val - actual_val)

    actual_hard_pass_rate = _rate(sum(1 for c in actual_hard_counts if c == 0), total)
    simulated_hard_pass_rate = _rate(
        sum(1 for c in simulated_hard_counts if c == 0), total
    )
    hard_pass_rate_change = simulated_hard_pass_rate - actual_hard_pass_rate

    sign_test_p = _sign_test_two_sided_p(wins=wins, losses=losses)
    win_rate_no_ties = _rate(wins, wins + losses)
    non_regression_rate = _rate(wins + ties, total)
    human_writer_pass_rate_actual = _rate(actual_human_writer_passes, total)
    human_writer_pass_rate_simulated = _rate(simulated_human_writer_passes, total)
    formatting_clean_pass_rate_actual = _rate(actual_formatting_clean_passes, total)
    formatting_clean_pass_rate_simulated = _rate(
        simulated_formatting_clean_passes, total
    )
    duplicate_link_issue_rate_actual = _rate(actual_duplicate_url_hard_total, total)
    duplicate_link_issue_rate_simulated = _rate(
        simulated_duplicate_url_hard_total, total
    )
    duplicate_story_signal_rate_actual = _rate(actual_duplicate_story_signals, total)
    duplicate_story_signal_rate_simulated = _rate(
        simulated_duplicate_story_signals, total
    )

    dimension_avg_delta = {
        dim: round(mean(values), 2) if values else 0.0
        for dim, values in dimension_deltas.items()
    }

    human_writer_pass_rate_change = (
        human_writer_pass_rate_simulated - human_writer_pass_rate_actual
    )
    formatting_clean_pass_rate_change = (
        formatting_clean_pass_rate_simulated - formatting_clean_pass_rate_actual
    )
    duplicate_link_issue_rate_change = (
        duplicate_link_issue_rate_simulated - duplicate_link_issue_rate_actual
    )
    duplicate_story_signal_rate_change = (
        duplicate_story_signal_rate_simulated - duplicate_story_signal_rate_actual
    )

    # Decision policy tuned for model-swap go/no-go calls.
    recommendation = "hold"
    if (
        avg_delta >= 1.5
        and wins >= losses + 2
        and hard_pass_rate_change >= 0.0
        and human_writer_pass_rate_change >= 0.0
        and formatting_clean_pass_rate_change >= 0.0
        and duplicate_link_issue_rate_change <= 0.0
        and sign_test_p <= 0.2
    ):
        recommendation = "promote"
    elif (
        avg_delta <= -1.5
        and losses >= wins + 2
        and (
            hard_pass_rate_change <= 0.0
            or human_writer_pass_rate_change < 0.0
            or formatting_clean_pass_rate_change < 0.0
            or duplicate_link_issue_rate_change > 0.0
        )
        and sign_test_p <= 0.2
    ):
        recommendation = "rollback"

    confidence = "low"
    if total >= 20 and sign_test_p <= 0.05 and abs(avg_delta) >= 2.0:
        confidence = "high"
    elif total >= 12 and sign_test_p <= 0.15 and abs(avg_delta) >= 1.0:
        confidence = "medium"

    rationale = (
        f"avg_delta={avg_delta:.2f}, wins/losses/ties={wins}/{losses}/{ties}, "
        f"human_writer_change={human_writer_pass_rate_change:+.3f}, "
        f"formatting_clean_change={formatting_clean_pass_rate_change:+.3f}, "
        f"duplicate_link_issue_change={duplicate_link_issue_rate_change:+.3f}, "
        f"sign_test_p={sign_test_p:.4f}"
    )

    return {
        "delta_distribution": {
            "median_delta": round(_percentile(sorted_deltas, 0.5), 2),
            "p10_delta": round(_percentile(sorted_deltas, 0.1), 2),
            "p90_delta": round(_percentile(sorted_deltas, 0.9), 2),
            "std_delta": round(pstdev(deltas), 2) if len(deltas) > 1 else 0.0,
        },
        "win_loss": {
            "wins": wins,
            "losses": losses,
            "ties": ties,
            "win_rate_no_ties": round(win_rate_no_ties, 3),
            "non_regression_rate": round(non_regression_rate, 3),
            "sign_test_p_value": sign_test_p,
        },
        "gate_quality": {
            "actual_hard_pass_rate": round(actual_hard_pass_rate, 3),
            "simulated_hard_pass_rate": round(simulated_hard_pass_rate, 3),
            "hard_pass_rate_change": round(hard_pass_rate_change, 3),
            "actual_avg_hard_issues": round(mean(actual_hard_counts), 2),
            "simulated_avg_hard_issues": round(mean(simulated_hard_counts), 2),
            "avg_hard_issue_change": round(
                mean(simulated_hard_counts) - mean(actual_hard_counts), 2
            ),
            "actual_missing_url_hard_total": actual_missing_url_hard_total,
            "simulated_missing_url_hard_total": simulated_missing_url_hard_total,
        },
        "focus_metrics": {
            "human_writer_pass_rate_actual": round(human_writer_pass_rate_actual, 3),
            "human_writer_pass_rate_simulated": round(
                human_writer_pass_rate_simulated, 3
            ),
            "human_writer_pass_rate_change": round(human_writer_pass_rate_change, 3),
            "formatting_clean_pass_rate_actual": round(
                formatting_clean_pass_rate_actual, 3
            ),
            "formatting_clean_pass_rate_simulated": round(
                formatting_clean_pass_rate_simulated, 3
            ),
            "formatting_clean_pass_rate_change": round(
                formatting_clean_pass_rate_change, 3
            ),
            "duplicate_link_issue_rate_actual": round(
                duplicate_link_issue_rate_actual, 3
            ),
            "duplicate_link_issue_rate_simulated": round(
                duplicate_link_issue_rate_simulated, 3
            ),
            "duplicate_link_issue_rate_change": round(
                duplicate_link_issue_rate_change, 3
            ),
            "duplicate_story_signal_rate_actual": round(
                duplicate_story_signal_rate_actual, 3
            ),
            "duplicate_story_signal_rate_simulated": round(
                duplicate_story_signal_rate_simulated, 3
            ),
            "duplicate_story_signal_rate_change": round(
                duplicate_story_signal_rate_change, 3
            ),
        },
        "dimension_avg_delta": dimension_avg_delta,
        "decision": {
            "recommendation": recommendation,
            "confidence": confidence,
            "rationale": rationale,
        },
    }


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
        depth_score = max(
            0, 15 - int((length - hard_max_chars) / max(1, hard_max_chars) * 15)
        )
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
        [body]
        + [str(i.get("title", "")) + " " + str(i.get("summary", "")) for i in items]
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
    if re.search(
        r"(?im)^\*\*(?:detection|source|threat|response|mitigation|intel)\s*:", body
    ):
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
        "novelty": 10,  # assessed by critic LLM, not heuristic
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
    writer: WriterWorkflow,
    items: list[dict],
    recent_briefings: list[dict],
    *,
    apply_critic_rewrites: bool,
) -> tuple[str, dict[str, object]]:
    return await writer.simulate_briefing_body(
        items,
        recent_briefings,
        apply_critic_rewrites=apply_critic_rewrites,
    )


def _seed_recurring_notes(results: list[dict[str, object]]) -> Counter[str]:
    notes: Counter[str] = Counter()
    for row in results:
        for note in row.get("actual_notes") or []:
            notes[str(note)] += 1
    return notes


def _build_simulation_report(
    *,
    candidate_overrides: dict[str, object],
    limit: int,
    since_days: int,
    include_text: bool,
    apply_critic_rewrites: bool,
    reanalyze_items: bool,
    results: list[dict[str, object]],
    recurring_notes: Counter[str],
) -> dict[str, object]:
    actual_scores = [int(row["actual_score"]) for row in results]
    simulated_scores = [int(row["simulated_score"]) for row in results]
    deltas = [int(row["delta"]) for row in results]

    actual_llm_tone_scores = [int(row["actual_llm_tone_score"]) for row in results]
    simulated_llm_tone_scores = [int(row["simulated_llm_tone_score"]) for row in results]
    actual_llm_novelty_scores = [int(row["actual_llm_novelty_score"]) for row in results]
    simulated_llm_novelty_scores = [
        int(row["simulated_llm_novelty_score"]) for row in results
    ]

    summary = {
        "avg_actual_score": round(mean(actual_scores), 2) if actual_scores else 0.0,
        "avg_simulated_score": round(mean(simulated_scores), 2) if simulated_scores else 0.0,
        "avg_actual_llm_tone_score": round(mean(actual_llm_tone_scores), 2)
        if actual_llm_tone_scores
        else 0.0,
        "avg_simulated_llm_tone_score": round(mean(simulated_llm_tone_scores), 2)
        if simulated_llm_tone_scores
        else 0.0,
        "avg_llm_tone_score_change": round(
            mean(simulated_llm_tone_scores) - mean(actual_llm_tone_scores),
            2,
        )
        if simulated_llm_tone_scores and actual_llm_tone_scores
        else 0.0,
        "avg_actual_llm_novelty_score": round(mean(actual_llm_novelty_scores), 2)
        if actual_llm_novelty_scores
        else 0.0,
        "avg_simulated_llm_novelty_score": round(
            mean(simulated_llm_novelty_scores),
            2,
        )
        if simulated_llm_novelty_scores
        else 0.0,
        "avg_llm_novelty_score_change": round(
            mean(simulated_llm_novelty_scores) - mean(actual_llm_novelty_scores),
            2,
        )
        if simulated_llm_novelty_scores and actual_llm_novelty_scores
        else 0.0,
        "avg_delta": round(mean(deltas), 2) if deltas else 0.0,
        "improved": sum(1 for delta in deltas if delta > 0),
        "regressed": sum(1 for delta in deltas if delta < 0),
        "equal": sum(1 for delta in deltas if delta == 0),
        "top_actual_feedback_gaps": [
            {"issue": issue, "count": count}
            for issue, count in recurring_notes.most_common(8)
        ],
    }
    summary.update(_build_decision_summary(results))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candidate_overrides": candidate_overrides,
        "count": len(results),
        "limit": int(limit),
        "since_days": int(since_days),
        "include_text": bool(include_text),
        "apply_critic_rewrites": bool(apply_critic_rewrites),
        "reanalyze_items": bool(reanalyze_items),
        "results": results,
        "summary": summary,
    }


async def simulate_historical_briefings(
    settings: Settings,
    *,
    limit: int = 0,
    since_days: int = 0,
    candidate_overrides_path: str | None = None,
    include_text: bool = False,
    apply_critic_rewrites: bool = True,
    reanalyze_items: bool = False,
    start_briefing_index: int = 0,
    existing_results: list[dict[str, object]] | None = None,
    progress_callback: SimulationProgressCallback | None = None,
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
    effective_settings, candidate_overrides = load_settings_with_overrides(
        settings,
        candidate_overrides_path,
    )
    writer = build_writer_workflow(effective_settings)
    analyst: AnalystWorkflowProtocol | None = None
    if reanalyze_items:
        analyst = AnalystService(effective_settings)

    results: list[dict[str, object]] = list(existing_results or [])
    recurring_notes = _seed_recurring_notes(results)

    try:
        for idx, briefing in enumerate(ordered[start_briefing_index:], start=start_briefing_index):
            item_ids = list(briefing.get("item_ids") or [])
            if not item_ids:
                continue
            item_rows = await get_items_by_ids(item_ids)
            items = _order_items_by_ids([dict(r) for r in item_rows], item_ids)
            if not items:
                continue

            if analyst and items:
                logger.info(
                    "Re-analyzing %d items for briefing %s to capture new Analyst features (e.g. canonical URLs)...",
                    len(items),
                    briefing["id"],
                )
                for db_item in items:
                    if db_item.get("source_type") in ("twitter", "reddit") and db_item.get(
                        "raw_data"
                    ):
                        update = await analyst.analyze_item(dict(db_item))
                        await update_item_analyzed(
                            item_id=db_item["id"],
                            summary=update.summary,
                            relevance_score=update.relevance_score,
                            ai_tags=update.ai_tags,
                            full_content=update.full_content,
                            image_prompt=update.image_prompt,
                            canonical_url=update.canonical_url,
                        )

                item_rows = await get_items_by_ids(item_ids)
                items = _order_items_by_ids([dict(r) for r in item_rows], item_ids)

            history_start = max(
                0,
                idx - int(effective_settings.briefing_history_items),
            )
            history = [
                {
                    "id": str(prev["id"]),
                    "content_markdown": _strip_cover_image(
                        str(prev.get("content_markdown") or "")
                    ),
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
            simulated_items = list(meta.get("selected_items") or items)

            mode = str(meta["mode"])

            actual_review = await writer.evaluate_existing_markdown(
                markdown=actual_body,
                selected_items=items,
                history=history,
                mode=mode,
            )
            simulated_review = await writer.evaluate_existing_markdown(
                markdown=simulated_body,
                selected_items=simulated_items,
                history=history,
                mode=mode,
            )

            actual_eval = score_feedback_rubric(
                actual_body,
                items,
                actual_review["gate"],
                min_chars=int(actual_review["min_chars"]),
                hard_max_chars=int(actual_review["hard_max_chars"]),
            )
            simulated_eval = score_feedback_rubric(
                simulated_body,
                simulated_items,
                simulated_review["gate"],
                min_chars=int(simulated_review["min_chars"]),
                hard_max_chars=int(simulated_review["hard_max_chars"]),
            )

            actual_style_score = actual_review.get("critique", {}).get("dimension_scores", {}).get(
                "style",
                0,
            )
            simulated_style_score = simulated_review.get("critique", {}).get("dimension_scores", {}).get(
                "style",
                0,
            )
            actual_novelty_score = actual_review.get("critique", {}).get("dimension_scores", {}).get(
                "novelty",
                0,
            )
            simulated_novelty_score = simulated_review.get("critique", {}).get("dimension_scores", {}).get(
                "novelty",
                0,
            )

            for note in actual_eval["notes"]:
                recurring_notes[str(note)] += 1

            delta = int(simulated_eval["score"]) - int(actual_eval["score"])
            entry: dict[str, object] = {
                "briefing_id": str(briefing["id"]),
                "created_at": briefing["created_at"].isoformat(),
                "item_count": len(items),
                "simulated_item_count": len(simulated_items),
                "mode": mode,
                "simulated_rewrites": int(meta["rewrites"]),
                "actual_score": int(actual_eval["score"]),
                "simulated_score": int(simulated_eval["score"]),
                "actual_llm_tone_score": int(actual_style_score),
                "simulated_llm_tone_score": int(simulated_style_score),
                "actual_llm_novelty_score": int(actual_novelty_score),
                "simulated_llm_novelty_score": int(simulated_novelty_score),
                "delta": delta,
                "actual_breakdown": actual_eval["breakdown"],
                "simulated_breakdown": simulated_eval["breakdown"],
                "actual_critic_dimension_scores": actual_review.get("critique", {}).get(
                    "dimension_scores",
                    {},
                ),
                "simulated_critic_dimension_scores": simulated_review.get("critique", {}).get(
                    "dimension_scores",
                    {},
                ),
                "actual_notes": actual_eval["notes"],
                "simulated_notes": simulated_eval["notes"],
                "actual_gate_hard_issues": [
                    str(issue) for issue in actual_review.get("gate", {}).get("hard_issues", [])
                ],
                "simulated_gate_hard_issues": [
                    str(issue)
                    for issue in simulated_review.get("gate", {}).get("hard_issues", [])
                ],
            }
            if include_text:
                entry["actual_markdown"] = actual_body
                entry["simulated_markdown"] = simulated_body
                entry["history"] = history
                entry["selected_items"] = items
                entry["simulated_selected_items"] = simulated_items
            results.append(entry)
            if progress_callback is not None:
                await progress_callback(
                    idx + 1,
                    _build_simulation_report(
                        candidate_overrides=candidate_overrides,
                        limit=limit,
                        since_days=since_days,
                        include_text=include_text,
                        apply_critic_rewrites=apply_critic_rewrites,
                        reanalyze_items=reanalyze_items,
                        results=results,
                        recurring_notes=recurring_notes,
                    ),
                )
            if (idx + 1) % 5 == 0:
                logger.info(
                    "Simulation progress: %d/%d briefings processed",
                    idx + 1,
                    len(ordered),
                )

        return _build_simulation_report(
            candidate_overrides=candidate_overrides,
            limit=limit,
            since_days=since_days,
            include_text=include_text,
            apply_critic_rewrites=apply_critic_rewrites,
            reanalyze_items=reanalyze_items,
            results=results,
            recurring_notes=recurring_notes,
        )
    finally:
        await writer.close()
        if analyst is not None:
            await analyst.close()


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

    current_decision = current_summary.get("decision")
    if not isinstance(current_decision, dict):
        current_decision = {}
    previous_decision = previous_summary.get("decision")
    if not isinstance(previous_decision, dict):
        previous_decision = {}
    current_gate = current_summary.get("gate_quality")
    if not isinstance(current_gate, dict):
        current_gate = {}
    previous_gate = previous_summary.get("gate_quality")
    if not isinstance(previous_gate, dict):
        previous_gate = {}
    current_focus = current_summary.get("focus_metrics")
    if not isinstance(current_focus, dict):
        current_focus = {}
    previous_focus = previous_summary.get("focus_metrics")
    if not isinstance(previous_focus, dict):
        previous_focus = {}

    current_results = _result_map(current_report)
    previous_results = _result_map(previous_report)
    current_ids = set(current_results.keys())
    previous_ids = set(previous_results.keys())
    overlap = sorted(current_ids & previous_ids)

    score_changes: list[tuple[str, int, int, int]] = []
    for briefing_id in overlap:
        current_score = int(current_results[briefing_id].get("simulated_score", 0))
        previous_score = int(previous_results[briefing_id].get("simulated_score", 0))
        score_changes.append(
            (briefing_id, current_score - previous_score, current_score, previous_score)
        )

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
        "avg_simulated_score_change": round(mean(score_delta_values), 2)
        if score_delta_values
        else 0.0,
        "avg_delta_change": round(
            float(current_summary.get("avg_delta", 0) or 0)
            - float(previous_summary.get("avg_delta", 0) or 0),
            2,
        ),
        "simulated_hard_pass_rate_change": round(
            float(current_gate.get("simulated_hard_pass_rate", 0) or 0)
            - float(previous_gate.get("simulated_hard_pass_rate", 0) or 0),
            3,
        ),
        "avg_hard_issue_change_delta": round(
            float(current_gate.get("avg_hard_issue_change", 0) or 0)
            - float(previous_gate.get("avg_hard_issue_change", 0) or 0),
            2,
        ),
        "human_writer_pass_rate_change": round(
            float(current_focus.get("human_writer_pass_rate_simulated", 0) or 0)
            - float(previous_focus.get("human_writer_pass_rate_simulated", 0) or 0),
            3,
        ),
        "formatting_clean_pass_rate_change": round(
            float(current_focus.get("formatting_clean_pass_rate_simulated", 0) or 0)
            - float(previous_focus.get("formatting_clean_pass_rate_simulated", 0) or 0),
            3,
        ),
        "duplicate_link_issue_rate_change": round(
            float(current_focus.get("duplicate_link_issue_rate_simulated", 0) or 0)
            - float(previous_focus.get("duplicate_link_issue_rate_simulated", 0) or 0),
            3,
        ),
        "improved_vs_previous": len(gains),
        "regressed_vs_previous": len(losses),
        "unchanged_vs_previous": len(equals),
        "baseline_decision": str(previous_decision.get("recommendation", "") or ""),
        "current_decision": str(current_decision.get("recommendation", "") or ""),
        "decision_changed": str(previous_decision.get("recommendation", "") or "")
        != str(current_decision.get("recommendation", "") or ""),
        "top_gains": _fmt(top_gains),
        "top_losses": _fmt(top_losses),
    }
