"""Release-review helpers for writer workflows."""

from __future__ import annotations

from typing import Any

from bcn.contracts.review import CritiqueRequest
from bcn.contracts.review import VerificationRequest

_CRITIC_BLOCKING_TERMS = (
    "factual overreach",
    "contradiction",
    "not in selected item",
    "not in selected items",
    "ungrounded",
    "hallucinat",
    "invalid advisory",
    "invalid link",
    "misleading claim",
)


def default_critique() -> dict[str, object]:
    """Return a permissive critic payload when the critic is disabled."""
    return {
        "passed": True,
        "score": 100,
        "dimension_scores": {
            "actionability": 100,
            "source_diversity": 100,
            "link_hygiene": 100,
            "clarity": 100,
            "style": 100,
            "novelty": 100,
        },
        "issues": [],
        "recommendations": [],
    }


def default_verifier() -> dict[str, object]:
    """Return a permissive verifier payload when the verifier is disabled."""
    return {
        "passed": True,
        "score": 100,
        "hard_issues": [],
        "blocking_hard_issues": [],
        "soft_issues": [],
        "issues": [],
        "recommendations": [],
    }


async def critique_markdown(
    service: Any,
    draft_markdown: str,
    items: list[dict[str, Any]],
    *,
    mode: str,
    recent_briefings: list[dict[str, Any]] | None = None,
    gate_hard_issues: list[str] | None = None,
    gate_soft_issues: list[str] | None = None,
) -> dict[str, Any]:
    """Run the critic or return a permissive default payload."""
    if not service.settings.briefing_critique_enabled:
        return default_critique()
    if service.critic_evaluator is None:
        raise RuntimeError(
            "WriterService requires a critic_evaluator when critique is enabled."
        )
    return await service.critic_evaluator.evaluate(
        CritiqueRequest(
            draft_markdown=draft_markdown,
            items=tuple(items),
            mode=mode,
            source="writer_service",
            recent_briefings=tuple(recent_briefings or []),
            gate_hard_issues=tuple(gate_hard_issues or []),
            gate_soft_issues=tuple(gate_soft_issues or []),
        )
    )


async def verify_markdown(
    service: Any,
    markdown: str,
    selected_items: list[dict[str, Any]],
    *,
    mode: str,
) -> dict[str, Any]:
    """Run factual verification or return a permissive default payload."""
    if not service.settings.briefing_verifier_enabled:
        return default_verifier()
    if service.verifier_evaluator is None:
        raise RuntimeError(
            "WriterService requires a verifier_evaluator when verification is enabled."
        )
    return await service.verifier_evaluator.evaluate(
        VerificationRequest(
            draft_markdown=markdown,
            items=tuple(selected_items),
            mode=mode,
            source="writer_service",
        )
    )


async def evaluate_existing_markdown(
    service: Any,
    *,
    markdown: str,
    selected_items: list[dict[str, Any]],
    history: list[dict[str, Any]],
    mode: str,
) -> dict[str, Any]:
    """Score one existing markdown draft against current release checks."""
    min_chars, target_chars, hard_max_chars = service.char_limits(
        mode,
        selected_count=len(selected_items),
    )
    normalized = service.normalize_section_headings(
        service.dedupe_markdown_links((markdown or "").strip())
    )
    normalized = service.de_template_fields(normalized)
    normalized = service.enforce_release_link_hygiene(
        normalized,
        selected_items,
        hard_max_chars=hard_max_chars,
    )

    gate = quality_gate(
        service,
        markdown=normalized,
        selected_items=selected_items,
        mode=mode,
        min_chars=min_chars,
        hard_max_chars=hard_max_chars,
    )
    critique = await critique_markdown(
        service,
        normalized,
        selected_items,
        mode=mode,
        gate_hard_issues=[str(issue) for issue in gate.get("hard_issues", [])],
        gate_soft_issues=[str(issue) for issue in gate.get("soft_issues", [])],
        recent_briefings=history,
    )
    verifier = await verify_markdown(
        service,
        normalized,
        selected_items,
        mode=mode,
    )
    critique_passed = passes_critic_thresholds(service, critique)
    release_passed = (
        bool(gate.get("passed", False))
        and critique_passed
        and bool(verifier.get("passed", True))
    )
    return {
        "markdown": normalized,
        "mode": mode,
        "min_chars": min_chars,
        "target_chars": target_chars,
        "hard_max_chars": hard_max_chars,
        "gate": gate,
        "critique": critique,
        "critic_threshold_passed": critique_passed,
        "verifier": verifier,
        "release_passed": release_passed,
    }


def quality_gate(
    service: Any,
    markdown: str,
    selected_items: list[dict[str, Any]],
    *,
    mode: str,
    min_chars: int,
    hard_max_chars: int,
) -> dict[str, object]:
    """Run deterministic release checks against markdown."""
    return service.quality.evaluate(
        markdown=markdown,
        selected_items=selected_items,
        mode=mode,
        min_chars=min_chars,
        hard_max_chars=hard_max_chars,
    )


def passes_critic_thresholds(service: Any, critique: dict[str, object]) -> bool:
    """Apply blocking thresholds for critic score and key dimensions."""
    if not critique:
        return False
    if not bool(critique.get("passed", False)):
        return False
    if has_critical_critic_issue(critique):
        return False

    score = int(critique.get("score", 0) or 0)
    dims = critique.get("dimension_scores", {}) or {}
    if not isinstance(dims, dict):
        dims = {}
    actionability = int(dims.get("actionability", 0) or 0)
    link_hygiene = int(dims.get("link_hygiene", 0) or 0)

    return (
        score >= int(service.settings.briefing_critic_min_score)
        and actionability >= int(service.settings.briefing_critic_min_actionability)
        and link_hygiene >= int(service.settings.briefing_critic_min_link_hygiene)
    )


def has_critical_critic_issue(critique: dict[str, object]) -> bool:
    """Return whether the critic payload contains a blocking issue."""
    issues = critique.get("issues", [])
    recommendations = critique.get("recommendations", [])
    payload: list[str] = []
    if isinstance(issues, list):
        payload.extend(str(item) for item in issues)
    elif issues:
        payload.append(str(issues))
    if isinstance(recommendations, list):
        payload.extend(str(item) for item in recommendations)
    elif recommendations:
        payload.append(str(recommendations))
    joined = " | ".join(text.lower() for text in payload if text)
    return any(term in joined for term in _CRITIC_BLOCKING_TERMS)


def build_rewrite_feedback_context(
    service: Any,
    *,
    gate: dict[str, object],
    critique: dict[str, object],
    verification: dict[str, object],
    mode: str,
    min_chars: int,
    target_chars: int,
    hard_max_chars: int,
    rewrite_attempt: int,
    max_rewrites: int,
    selected_items: list[dict[str, Any]],
    missing_selected_urls: list[str] | None = None,
) -> dict[str, Any]:
    """Build compact structured rewrite guidance for the LLM."""
    gate_hard = string_list(gate.get("hard_issues"), limit=16)
    gate_soft = string_list(gate.get("soft_issues"), limit=16)
    gate_issues = string_list(gate.get("issues"), limit=20)
    critic_issues = string_list(critique.get("issues"), limit=16)
    critic_recommendations = string_list(
        critique.get("recommendations"),
        limit=16,
    )
    verifier_hard = string_list(verification.get("hard_issues"), limit=16)
    verifier_blocking_hard = string_list(
        verification.get("blocking_hard_issues"),
        limit=16,
    )
    verifier_soft = string_list(verification.get("soft_issues"), limit=16)
    verifier_recommendations = string_list(
        verification.get("recommendations"),
        limit=16,
    )

    critic_dims = critique.get("dimension_scores", {})
    if not isinstance(critic_dims, dict):
        critic_dims = {}
    min_thresholds = {
        "score": int(service.settings.briefing_critic_min_score),
        "actionability": int(service.settings.briefing_critic_min_actionability),
        "link_hygiene": int(service.settings.briefing_critic_min_link_hygiene),
    }
    failed_critic_thresholds: list[str] = []
    critic_score = int(critique.get("score", 0) or 0)
    if critic_score < min_thresholds["score"]:
        failed_critic_thresholds.append(
            f"score {critic_score} < {min_thresholds['score']}"
        )
    for dimension in ("actionability", "link_hygiene"):
        dim_score = int(critic_dims.get(dimension, 0) or 0)
        if dim_score < min_thresholds[dimension]:
            failed_critic_thresholds.append(
                f"{dimension} {dim_score} < {min_thresholds[dimension]}"
            )

    compact_items: list[dict[str, Any]] = []
    for item in selected_items[:12]:
        compact_items.append(
            {
                "url": str(item.get("url", "")).strip(),
                "title": str(item.get("title", "")).strip(),
                "source_type": str(item.get("source_type", "")).strip(),
                "relevance_score": int(item.get("relevance_score", 0) or 0),
            }
        )

    priorities: list[str] = []
    if gate_hard:
        priorities.append("Resolve gate hard issues first (blocking release).")
    if verifier_blocking_hard:
        priorities.append(
            "Resolve verifier deterministic blocking issues before style changes."
        )
    if verifier_hard:
        priorities.append(
            "Address verifier hard issues to tighten factual grounding."
        )
    if failed_critic_thresholds:
        priorities.append(
            "Raise critic threshold failures: "
            + "; ".join(failed_critic_thresholds[:3])
        )
    if not priorities:
        priorities.append(
            "Improve clarity and actionability while preserving exact URL coverage."
        )

    return {
        "rewrite": {
            "attempt": int(max(1, rewrite_attempt)),
            "max_attempts": int(max(0, max_rewrites)),
            "mode": mode,
            "target_length_chars": {
                "min": int(min_chars),
                "target": int(target_chars),
                "hard_max": int(hard_max_chars),
            },
        },
        "release_status": {
            "gate_passed": bool(gate.get("passed", False)),
            "critic_passed": passes_critic_thresholds(service, critique),
            "verifier_passed": bool(verification.get("passed", True)),
        },
        "priority_order": priorities[:6],
        "blocking": {
            "gate_hard_issues": gate_hard,
            "verifier_blocking_hard_issues": verifier_blocking_hard,
            "verifier_hard_issues": verifier_hard,
        },
        "gate": {
            "issues": gate_issues,
            "soft_issues": gate_soft,
        },
        "critic": {
            "passed": bool(critique.get("passed", False)),
            "score": critic_score,
            "dimension_scores": {
                "actionability": int(critic_dims.get("actionability", 0) or 0),
                "source_diversity": int(
                    critic_dims.get("source_diversity", 0) or 0
                ),
                "link_hygiene": int(critic_dims.get("link_hygiene", 0) or 0),
                "clarity": int(critic_dims.get("clarity", 0) or 0),
                "style": int(critic_dims.get("style", 0) or 0),
            },
            "thresholds": min_thresholds,
            "failed_thresholds": failed_critic_thresholds,
            "issues": critic_issues,
            "recommendations": critic_recommendations,
        },
        "verifier": {
            "passed": bool(verification.get("passed", True)),
            "score": int(verification.get("score", 0) or 0),
            "soft_issues": verifier_soft,
            "recommendations": verifier_recommendations,
        },
        "coverage": {
            "missing_selected_urls": [
                str(url).strip()
                for url in (missing_selected_urls or [])
                if str(url).strip()
            ][:16],
            "selected_items": compact_items,
        },
    }


def build_preference_rationale(feedback: list[str] | None) -> str:
    """Summarize rewrite feedback for preference-pair training rows."""
    normalized = [
        str(item).strip().rstrip(".")
        for item in (feedback or [])
        if str(item).strip()
    ]
    if not normalized:
        return "Rewrite preferred based on aggregate release feedback"

    summary = "; ".join(normalized[:3])
    if len(normalized) > 3:
        summary += "; additional release feedback"
    return summary[:400]


def string_list(value: object, *, limit: int = 16) -> list[str]:
    """Normalize arbitrary payload values into a short string list."""
    if isinstance(value, list):
        raw = value
    elif value is None:
        raw = []
    else:
        raw = [value]
    out: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text:
            out.append(text)
        if len(out) >= limit:
            break
    return out


__all__ = [
    "build_preference_rationale",
    "build_rewrite_feedback_context",
    "critique_markdown",
    "default_critique",
    "default_verifier",
    "evaluate_existing_markdown",
    "has_critical_critic_issue",
    "passes_critic_thresholds",
    "quality_gate",
    "string_list",
    "verify_markdown",
]
