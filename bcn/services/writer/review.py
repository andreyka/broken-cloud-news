"""Release-review helpers for writer workflows."""

from __future__ import annotations

import re
from typing import Any

from bcn.briefing.story_identity import normalize_story_title
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
_REPEATED_TOPIC_RE = re.compile(
    r"^\s*repeated topic:\s*(.+?)(?:\s*\(.*\))?\.?\s*$",
    flags=re.IGNORECASE,
)
_REPEATED_TOPIC_RECOMMENDATION_RE = re.compile(
    r"^\s*(?:drop|remove)\s+(?:the\s+)?(.+?)(?:\s+(?:segment|reference|item|section))?(?:[;,.].*(?:already covered|covered in briefing).*)$",
    flags=re.IGNORECASE,
)
_REPEAT_TOPIC_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "already",
        "as",
        "at",
        "be",
        "briefing",
        "covered",
        "in",
        "of",
        "on",
        "or",
        "the",
        "topic",
        "via",
        "was",
        "were",
        "with",
    }
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


def normalize_critique_payload(payload: dict[str, object] | None) -> dict[str, object]:
    """Normalize critic-service outputs into the writer's internal shape."""
    critique = dict(payload or {})
    if "score" not in critique and "critic_score" in critique:
        critique["score"] = critique.get("critic_score")
    if "passed" not in critique and "critic_passed" in critique:
        critique["passed"] = critique.get("critic_passed")
    if "dimension_scores" not in critique and "critic_dimension_scores" in critique:
        critique["dimension_scores"] = critique.get("critic_dimension_scores")
    if "issues" not in critique and "critic_issues" in critique:
        critique["issues"] = critique.get("critic_issues")
    critique.setdefault("recommendations", [])
    critique.setdefault("issues", [])
    critique.setdefault("dimension_scores", {})
    return critique


def normalize_verifier_payload(payload: dict[str, object] | None) -> dict[str, object]:
    """Normalize verifier-service outputs into the writer's internal shape."""
    verifier = dict(payload or {})
    if "score" not in verifier and "verifier_score" in verifier:
        verifier["score"] = verifier.get("verifier_score")
    if "passed" not in verifier and "verifier_passed" in verifier:
        verifier["passed"] = verifier.get("verifier_passed")
    verifier.setdefault("issues", [])
    verifier.setdefault("recommendations", [])
    return verifier


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
    raw = await service.critic_evaluator.evaluate(
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
    return normalize_critique_payload(raw)


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
    raw = await service.verifier_evaluator.evaluate(
        VerificationRequest(
            draft_markdown=markdown,
            items=tuple(selected_items),
            mode=mode,
            source="writer_service",
        )
    )
    return normalize_verifier_payload(raw)


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


def _repeat_topic_tokens(text: str) -> tuple[str, ...]:
    """Normalize one repeated-topic label into stable matching tokens."""
    normalized = normalize_story_title(text)
    if not normalized:
        return ()
    tokens: list[str] = []
    for token in re.findall(r"[a-z0-9]{2,}", normalized):
        if token.isdigit() or token in _REPEAT_TOPIC_STOPWORDS:
            continue
        if token not in tokens:
            tokens.append(token)
    return tuple(tokens)


def _topic_overlap(text: str, topic_tokens: tuple[str, ...]) -> int:
    """Return the count of overlapping topic tokens for one text body."""
    if not text or not topic_tokens:
        return 0
    text_tokens = set(_repeat_topic_tokens(text))
    if not text_tokens:
        return 0
    return len(text_tokens & set(topic_tokens))


def _topic_match_threshold(topic_tokens: tuple[str, ...]) -> int:
    """Return the minimum overlap needed to treat a topic match as credible."""
    return 1 if len(topic_tokens) <= 1 else 2


def extract_repeated_topics(critique: dict[str, object]) -> list[str]:
    """Extract repeated-topic labels from critic issues."""
    topics: list[str] = []
    for issue in string_list(critique.get("issues"), limit=24):
        match = _REPEATED_TOPIC_RE.match(issue)
        if not match:
            continue
        topic = match.group(1).strip(" .")
        if topic and topic not in topics:
            topics.append(topic)
    for recommendation in string_list(critique.get("recommendations"), limit=24):
        match = _REPEATED_TOPIC_RECOMMENDATION_RE.match(recommendation)
        if not match:
            continue
        topic = match.group(1).strip(" .")
        if topic and topic not in topics:
            topics.append(topic)
    return topics


def extract_sticky_rewrite_constraints(
    critique: dict[str, object],
    verification: dict[str, object],
) -> list[str]:
    """Derive rewrite constraints that must persist across later rounds."""
    texts: list[str] = []
    texts.extend(string_list(critique.get("issues"), limit=24))
    texts.extend(string_list(critique.get("recommendations"), limit=24))
    texts.extend(string_list(verification.get("issues"), limit=24))
    texts.extend(string_list(verification.get("hard_issues"), limit=24))
    texts.extend(string_list(verification.get("blocking_hard_issues"), limit=24))
    texts.extend(string_list(verification.get("soft_issues"), limit=24))
    texts.extend(string_list(verification.get("recommendations"), limit=24))

    constraints: list[str] = []

    def add_constraint(text: str) -> None:
        normalized = text.strip()
        if normalized and normalized not in constraints:
            constraints.append(normalized)

    for text in texts:
        lowered = text.lower()
        if "factual overreach" in lowered:
            add_constraint(
                "Do not claim active exploitation, attacker usage, or in-the-wild abuse unless the selected source explicitly says so. If the source only proves capability, write 'can', 'lets an attacker', or 'could'."
            )
        if "assumption:" in lowered or (
            "source" in lowered
            and ("not provided" in lowered or "not in the source" in lowered)
            and ("payload" in lowered or "field" in lowered or "semicolon" in lowered)
        ):
            add_constraint(
                "Do not invent exploit payload mechanics, punctuation, field names, or step-by-step details unless the selected source explicitly states them. Keep exploit descriptions at the supported level."
            )
        if (
            "boilerplate" in lowered
            or "start directly" in lowered
            or "opener" in lowered
            or "welcome to another week" in lowered
            or "another day" in lowered
        ):
            add_constraint(
                "Do not use recurring opener boilerplate like 'Welcome to another week' or 'Another day'. Start directly with a factual thesis sentence."
            )

    return constraints


def trim_repeated_selected_items(
    service: Any,
    *,
    selected_items: list[dict[str, Any]],
    critique: dict[str, object],
    history: list[dict[str, Any]],
) -> dict[str, Any]:
    """Drop critic-flagged repeat topics when recent briefing history supports it."""
    repeated_topics = extract_repeated_topics(critique)
    if not repeated_topics or not selected_items or not history:
        return {
            "selected_items": list(selected_items),
            "dropped_items": [],
            "matched_topics": {},
        }

    supported_topics: list[tuple[str, tuple[str, ...]]] = []
    for topic in repeated_topics:
        topic_tokens = _repeat_topic_tokens(topic)
        if not topic_tokens:
            continue
        threshold = _topic_match_threshold(topic_tokens)
        if any(
            _topic_overlap(
                str(briefing.get("content_markdown") or ""),
                topic_tokens,
            )
            >= threshold
            for briefing in history
        ):
            supported_topics.append((topic, topic_tokens))

    if not supported_topics:
        return {
            "selected_items": list(selected_items),
            "dropped_items": [],
            "matched_topics": {},
        }

    kept_items: list[dict[str, Any]] = []
    dropped_items: list[dict[str, Any]] = []
    matched_topics: dict[str, list[str]] = {}
    for item in selected_items:
        item_text = " ".join(
            str(part or "")
            for part in (
                item.get("title"),
                item.get("summary"),
                item.get("url"),
            )
        )
        item_matches = [
            topic
            for topic, topic_tokens in supported_topics
            if _topic_overlap(item_text, topic_tokens)
            >= _topic_match_threshold(topic_tokens)
        ]
        if item_matches:
            dropped_items.append(item)
            matched_topics[str(item.get("id") or item.get("url") or item.get("title"))] = (
                item_matches
            )
        else:
            kept_items.append(item)

    if not dropped_items or not kept_items:
        return {
            "selected_items": list(selected_items),
            "dropped_items": [],
            "matched_topics": {},
        }

    return {
        "selected_items": kept_items,
        "dropped_items": dropped_items,
        "matched_topics": matched_topics,
    }


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
    sticky_constraints: list[str] | None = None,
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
        "sticky_constraints": [
            str(item).strip()
            for item in (sticky_constraints or [])
            if str(item).strip()
        ][:12],
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
    "extract_repeated_topics",
    "default_critique",
    "default_verifier",
    "evaluate_existing_markdown",
    "has_critical_critic_issue",
    "extract_sticky_rewrite_constraints",
    "normalize_critique_payload",
    "normalize_verifier_payload",
    "passes_critic_thresholds",
    "quality_gate",
    "string_list",
    "trim_repeated_selected_items",
    "verify_markdown",
]
