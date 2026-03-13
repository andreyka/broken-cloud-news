"""Scoring and guardrails for offline optimization runs."""

from __future__ import annotations

from typing import Any


def _num(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def score_optimization_candidate(
    *,
    champion_replay: dict[str, Any],
    candidate_replay: dict[str, Any],
    benchmark_report: dict[str, Any],
) -> dict[str, Any]:
    """Compare one candidate against champion replay + benchmark results."""
    champion_summary = (
        champion_replay.get("summary", {}) if isinstance(champion_replay, dict) else {}
    )
    candidate_summary = (
        candidate_replay.get("summary", {}) if isinstance(candidate_replay, dict) else {}
    )
    benchmark_summary = (
        benchmark_report.get("summary", {}) if isinstance(benchmark_report, dict) else {}
    )
    champion_gate = (
        champion_summary.get("gate_quality", {})
        if isinstance(champion_summary, dict)
        else {}
    )
    candidate_gate = (
        candidate_summary.get("gate_quality", {})
        if isinstance(candidate_summary, dict)
        else {}
    )
    champion_focus = (
        champion_summary.get("focus_metrics", {})
        if isinstance(champion_summary, dict)
        else {}
    )
    candidate_focus = (
        candidate_summary.get("focus_metrics", {})
        if isinstance(candidate_summary, dict)
        else {}
    )

    benchmark_delta = _num(benchmark_summary.get("candidate_vs_champion_case_pass_delta"))
    champion_replay_score = _num(champion_summary.get("avg_simulated_score"))
    candidate_replay_score = _num(candidate_summary.get("avg_simulated_score"))
    replay_score_delta = candidate_replay_score - champion_replay_score
    champion_hard_pass = _num(champion_gate.get("simulated_hard_pass_rate"))
    candidate_hard_pass = _num(candidate_gate.get("simulated_hard_pass_rate"))
    hard_pass_delta = candidate_hard_pass - champion_hard_pass
    champion_human_writer = _num(champion_focus.get("human_writer_pass_rate_simulated"))
    candidate_human_writer = _num(candidate_focus.get("human_writer_pass_rate_simulated"))
    human_writer_delta = candidate_human_writer - champion_human_writer
    champion_formatting = _num(champion_focus.get("formatting_clean_pass_rate_simulated"))
    candidate_formatting = _num(candidate_focus.get("formatting_clean_pass_rate_simulated"))
    formatting_delta = candidate_formatting - champion_formatting
    champion_dup_issue = _num(champion_focus.get("duplicate_link_issue_rate_simulated"))
    candidate_dup_issue = _num(candidate_focus.get("duplicate_link_issue_rate_simulated"))
    duplicate_issue_delta = candidate_dup_issue - champion_dup_issue
    champion_dup_story = _num(champion_focus.get("duplicate_story_signal_rate_simulated"))
    candidate_dup_story = _num(candidate_focus.get("duplicate_story_signal_rate_simulated"))
    duplicate_story_delta = candidate_dup_story - champion_dup_story

    hard_reject_reasons: list[str] = []
    if benchmark_delta < -0.02:
        hard_reject_reasons.append("benchmark_case_pass_regressed")
    if hard_pass_delta < 0:
        hard_reject_reasons.append("replay_hard_pass_regressed")
    if human_writer_delta < 0:
        hard_reject_reasons.append("replay_human_writer_regressed")
    if formatting_delta < 0:
        hard_reject_reasons.append("replay_formatting_clean_regressed")
    if duplicate_issue_delta > 0:
        hard_reject_reasons.append("replay_duplicate_link_issue_increased")
    if duplicate_story_delta > 0:
        hard_reject_reasons.append("replay_duplicate_story_signal_increased")

    composite_score = (
        (benchmark_delta * 100.0 * 0.45)
        + (replay_score_delta * 0.25)
        + (human_writer_delta * 100.0 * 0.15)
        + (formatting_delta * 100.0 * 0.10)
        - (max(0.0, duplicate_issue_delta) * 100.0 * 0.05)
    )

    recommendation = "reject" if hard_reject_reasons else "hold"
    if (
        not hard_reject_reasons
        and benchmark_delta >= 0
        and replay_score_delta >= 0
        and human_writer_delta >= 0
        and formatting_delta >= 0
    ):
        recommendation = "promote_candidate"
    elif not hard_reject_reasons:
        recommendation = "eligible"

    return {
        "hard_reject": bool(hard_reject_reasons),
        "hard_reject_reasons": hard_reject_reasons,
        "recommendation": recommendation,
        "composite_score": round(composite_score, 3),
        "metrics": {
            "benchmark_case_pass_delta": round(benchmark_delta, 3),
            "replay_avg_simulated_score_delta": round(replay_score_delta, 3),
            "replay_hard_pass_rate_delta": round(hard_pass_delta, 3),
            "replay_human_writer_pass_rate_delta": round(human_writer_delta, 3),
            "replay_formatting_clean_pass_rate_delta": round(formatting_delta, 3),
            "replay_duplicate_link_issue_rate_delta": round(duplicate_issue_delta, 3),
            "replay_duplicate_story_signal_rate_delta": round(
                duplicate_story_delta,
                3,
            ),
        },
        "benchmark_summary": benchmark_summary,
        "champion_replay_summary": champion_summary,
        "candidate_replay_summary": candidate_summary,
    }
