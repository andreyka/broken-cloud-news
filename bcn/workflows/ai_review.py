"""Automatic editorial AI review for distributed briefings."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from typing import Any
from uuid import UUID

import httpx

from bcn.common.config import Settings
from bcn.persistence.training import get_briefing_review_context
from bcn.persistence.training import has_ai_review
from bcn.persistence.training import insert_ai_review

REVIEW_DECISIONS = ("accept", "reject", "edit", "needs_work")
REVIEW_ISSUE_TAGS = (
    "factual_error",
    "unsupported_claim",
    "weak_cloud_focus",
    "weak_actionability",
    "weak_opener",
    "poor_structure",
    "formatting",
    "duplicate_url",
    "repeated_topic",
    "tone",
)
_OPENAI_API_BASE_URL = "https://api.openai.com/v1"


@dataclass(slots=True)
class AIReviewConfig:
    enabled: bool
    provider: str
    base_url: str
    model: str
    reasoning_effort: str | None
    api_key: str
    timeout_seconds: int


@dataclass(slots=True)
class AIReviewInput:
    briefing_id: UUID | None
    content_markdown: str
    latest_run_model: str | None = None
    latest_run_decision: str | None = None
    rewrite_count: int | None = None
    selected_items_context: list[dict[str, Any]] | None = None


@dataclass(slots=True)
class AIReviewResult:
    reviewer_provider: str
    reviewer_model: str
    reasoning_effort: str | None
    decision: str
    issue_tags: list[str]
    notes: str | None
    edited_markdown: str | None
    raw_response: dict[str, Any]


@dataclass(slots=True)
class AIPublishGateResult:
    action: str
    markdown: str
    reason: str
    review: AIReviewResult | None = None

    def as_gate_payload(self) -> dict[str, Any]:
        review = self.review
        return {
            "passed": self.action != "block",
            "action": self.action,
            "reason": self.reason,
            "decision": review.decision if review is not None else "error",
            "reviewer_provider": (
                review.reviewer_provider if review is not None else "openai"
            ),
            "reviewer_model": review.reviewer_model if review is not None else "",
            "reasoning_effort": (
                review.reasoning_effort if review is not None else None
            ),
            "issue_tags": list(review.issue_tags) if review is not None else [],
            "notes": review.notes if review is not None else None,
        }


def _env_value(name: str) -> str | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def get_ai_review_config(settings: Settings) -> AIReviewConfig:
    api_key = str(settings.ai_review_api_key or "").strip() or _env_value("OPENAI_API_KEY") or ""
    effort = str(settings.ai_review_reasoning_effort or "").strip().lower() or None
    return AIReviewConfig(
        enabled=bool(api_key),
        provider="openai",
        base_url=str(settings.ai_review_base_url or _OPENAI_API_BASE_URL).rstrip("/"),
        model=str(settings.ai_review_model or "gpt-5.4").strip(),
        reasoning_effort=effort,
        api_key=api_key,
        timeout_seconds=max(1, int(settings.ai_review_timeout_seconds or 180)),
    )


def _build_system_prompt() -> str:
    return "\n".join(
        [
            "You are a strict security editorial reviewer for a cloud security news briefing.",
            "You are not the original writer. You are the reviewer and rewrite editor.",
            "Review the supplied final BCN markdown as if you were deciding whether it should stand as-is, be lightly rewritten, need substantial rework, or be rejected.",
            "Return a single JSON object that matches the requested review schema.",
            "Allowed decisions:",
            "- accept: strong, publishable, no rewrite needed.",
            "- edit: mostly good; a rewrite improves accuracy, clarity, or formatting.",
            "- needs_work: useful, but not ready without substantive fixes.",
            "- reject: do not use this draft as a publish candidate.",
            "Rules:",
            "- Judge only from the provided markdown, metadata, and any selected-item context supplied with the request.",
            "- Do not invent facts, URLs, incidents, or organizations.",
            "- Treat unsupported certainty conservatively. If a claim appears stronger than the draft justifies, say so.",
            "- Distinguish between confirmed exploitation, proof-of-concept exploitation, theoretical impact, configuration-dependent risk, default-only risk, exposed-only risk, and authenticated versus unauthenticated attack paths when the draft suggests them.",
            "- Flag phrases that sound absolute or overly strong unless the text clearly justifies them.",
            "- Evaluate whether the draft truly reads as cloud security news, not just generic infrastructure security.",
            "- Strong cloud framing usually connects to control plane risk, identity or auth plane, secrets or metadata exposure, multi-tenancy, internet-facing edge risk, CI/CD supply chain, orchestration state, IAM boundaries, managed service exposure, or hybrid trust boundaries.",
            "- Keep the style punchy, technical, compact, and opinionated without fearmongering or generic filler.",
            "- Preserve existing links in any rewrite unless the surrounding sentence must be removed.",
            "- Prefer the smallest set of issue tags that explains the main problems.",
            "- The verdict summary and notes should be concise and concrete.",
            "- If you provide edited_markdown, it must be the full corrected markdown, not partial fragments.",
            "- If you provide alternate_markdown, it should be a second full draft with slightly different style: either more restrained and analytical or more punchy and digest-like.",
        ]
    )


def _render_selected_items_context(items: list[dict[str, Any]] | None) -> str | None:
    if not isinstance(items, list) or not items:
        return None
    rendered: list[str] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        lines = [f"Item {index}"]
        title = _as_string(item.get("title"))
        if title:
            lines.append(f"Title: {title}")
        summary = _as_string(item.get("summary"))
        if summary:
            lines.append(f"Summary: {summary}")
        source = _as_string(item.get("source"))
        if source:
            lines.append(f"Source: {source}")
        url = _as_string(item.get("url"))
        if url:
            lines.append(f"URL: {url}")
        if len(lines) > 1:
            rendered.append("\n".join(lines))
    if not rendered:
        return None
    return "\n\n".join(rendered)


def _build_user_prompt(input_data: AIReviewInput) -> str:
    briefing_label = str(input_data.briefing_id) if input_data.briefing_id else "pre-publish draft"
    item_context = _render_selected_items_context(input_data.selected_items_context)
    prompt_lines = [
        f"Briefing ID: {briefing_label}",
        f"Latest model: {input_data.latest_run_model or 'unknown'}",
        f"Latest generation decision: {input_data.latest_run_decision or 'unknown'}",
        f"Rewrite count: {int(input_data.rewrite_count or 0)}",
        "",
        "Evaluate the markdown below against BCN editorial standards:",
        "- factual grounding and scope accuracy",
        "- whether attack prerequisites are stated clearly",
        "- whether impact is overstated or understated",
        "- whether remediation advice is appropriately scoped",
        "- cloud relevance and cloud-security framing strength",
        "- opener strength, structure, readability, and tone precision",
    ]
    if item_context:
        prompt_lines.extend(
            [
                "",
                "Selected item context (treat this as the factual grounding for the review and rewrite):",
                item_context,
            ]
        )
    prompt_lines.extend(
        [
            "",
            "Markdown:",
            input_data.content_markdown,
        ]
    )
    return "\n".join(prompt_lines)


def _review_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "decision": {"type": "string", "enum": list(REVIEW_DECISIONS)},
            "issue_tags": {
                "type": "array",
                "items": {"type": "string", "enum": list(REVIEW_ISSUE_TAGS)},
            },
            "verdict_summary": {"type": "string"},
            "notes": {"type": "string"},
            "recommended_fixes": {
                "type": "array",
                "items": {"type": "string"},
            },
            "cloud_angle_strength": {
                "type": "string",
                "enum": ["strong", "moderate", "weak", "mostly_absent"],
            },
            "cloud_angle_rationale": {"type": "string"},
            "strong_claims_to_soften": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "original": {"type": "string"},
                        "why": {"type": "string"},
                        "safer_replacement": {"type": "string"},
                    },
                    "required": ["original", "why", "safer_replacement"],
                },
            },
            "assumptions": {
                "type": "array",
                "items": {"type": "string"},
            },
            "edited_markdown": {"type": "string"},
            "alternate_markdown": {"type": "string"},
        },
        "required": [
            "decision",
            "issue_tags",
            "verdict_summary",
            "notes",
            "cloud_angle_strength",
            "cloud_angle_rationale",
            "assumptions",
        ],
    }


def _response_output_text(response: dict[str, Any]) -> str | None:
    output_text = response.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text
    output = response.get("output")
    if not isinstance(output, list):
        return None
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "output_text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    return text
    return None


def _parse_json_like(value: str) -> dict[str, Any] | None:
    trimmed = str(value or "").strip()
    if not trimmed:
        return None
    candidates = [trimmed]
    if "```" in trimmed:
        fence_start = trimmed.find("```")
        fence_end = trimmed.rfind("```")
        if fence_start != -1 and fence_end > fence_start:
            fenced = trimmed[fence_start + 3 : fence_end].strip()
            if fenced.lower().startswith("json"):
                fenced = fenced[4:].strip()
            if fenced:
                candidates.insert(0, fenced)
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _as_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


def _sanitize_issue_tags(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    allowed = set(REVIEW_ISSUE_TAGS)
    tags: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        trimmed = item.strip()
        if trimmed in allowed and trimmed not in seen:
            tags.append(trimmed)
            seen.add(trimmed)
    return tags


def _sanitize_decision(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    if trimmed in REVIEW_DECISIONS:
        return trimmed
    return None


def _normalize_ai_review_result(
    config: AIReviewConfig,
    response: dict[str, Any],
    payload: dict[str, Any],
) -> AIReviewResult:
    decision = _sanitize_decision(payload.get("decision"))
    if not decision:
        raise RuntimeError("AI review did not return a supported decision.")
    edited_markdown = _as_string(payload.get("edited_markdown"))
    reviewer_model = _as_string(response.get("model")) or config.model
    response_id = _as_string(response.get("id"))
    usage = response.get("usage")
    return AIReviewResult(
        reviewer_provider="openai",
        reviewer_model=reviewer_model,
        reasoning_effort=config.reasoning_effort,
        decision=decision,
        issue_tags=_sanitize_issue_tags(payload.get("issue_tags")),
        notes=_as_string(payload.get("notes")),
        edited_markdown=edited_markdown,
        raw_response={
            "response_id": response_id,
            "model": reviewer_model,
            "usage": usage if isinstance(usage, dict) else {},
            "extracted_review": payload,
        },
    )


async def _request_ai_review_response(
    config: AIReviewConfig,
    input_data: AIReviewInput,
    *,
    structured: bool,
) -> dict[str, Any]:
    instructions = (
        _build_system_prompt()
        if structured
        else _build_system_prompt()
        + "\nReturn only one JSON object and no surrounding prose or markdown fences."
    )
    payload: dict[str, Any] = {
        "model": config.model,
        # Reasoning tokens share this budget on /v1/responses; xhigh effort
        # can exhaust a small cap before the JSON is emitted.
        "max_output_tokens": 32000,
        "instructions": instructions,
        "input": _build_user_prompt(input_data),
    }
    if config.reasoning_effort:
        payload["reasoning"] = {"effort": config.reasoning_effort}
    if structured:
        payload["text"] = {
            "format": {
                "type": "json_schema",
                "name": "briefing_review",
                "schema": _review_json_schema(),
                "strict": True,
            }
        }
    timeout = httpx.Timeout(float(config.timeout_seconds), connect=min(30.0, float(config.timeout_seconds)))
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{config.base_url}/responses",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.api_key}",
            },
            json=payload,
        )
    if not response.is_success:
        body = response.text
        if structured and response.status_code == 400:
            lowered = body.lower()
            if (
                "structured outputs" in lowered
                or "json_schema" in lowered
                or "text.format" in lowered
            ):
                return await _request_ai_review_response(
                    config,
                    input_data,
                    structured=False,
                )
        raise RuntimeError(
            f"OpenAI review request failed ({response.status_code}): {body[:400]}"
        )
    parsed = response.json()
    if not isinstance(parsed, dict):
        raise RuntimeError("OpenAI review did not return a JSON object response.")
    return parsed


async def run_openai_editorial_review(
    settings: Settings,
    input_data: AIReviewInput,
) -> AIReviewResult:
    config = get_ai_review_config(settings)
    if not config.enabled or not config.api_key:
        raise RuntimeError(
            "AI review is not configured. Set BCN_AI_REVIEW_API_KEY or OPENAI_API_KEY."
        )
    response = await _request_ai_review_response(config, input_data, structured=True)
    extracted = _parse_json_like(_response_output_text(response) or "")
    if extracted is None:
        raise RuntimeError("OpenAI review did not return a parsable JSON result.")
    return _normalize_ai_review_result(config, response, extracted)


async def run_prepublish_ai_review_gate(
    settings: Settings,
    *,
    content_markdown: str,
    selected_items: list[dict[str, Any]],
    latest_run_model: str | None = None,
    latest_run_decision: str | None = "PUBLISHED",
    rewrite_count: int | None = None,
) -> AIPublishGateResult:
    """Run the AI editor before publish and convert it into a strict gate decision."""
    review = await run_openai_editorial_review(
        settings,
        AIReviewInput(
            briefing_id=None,
            content_markdown=content_markdown,
            latest_run_model=latest_run_model,
            latest_run_decision=latest_run_decision,
            rewrite_count=rewrite_count,
            selected_items_context=selected_items,
        ),
    )
    if review.decision == "accept":
        return AIPublishGateResult(
            action="approve",
            markdown=content_markdown,
            reason="AI editorial gate accepted the draft.",
            review=review,
        )
    if review.decision in ("edit", "needs_work"):
        # Both decisions usually ship a full rewrite; applying it and
        # re-running the release checks beats vetoing a passing draft over
        # fixes the editor already made. Only reject stays a hard veto.
        rewritten = _as_string(review.edited_markdown)
        if rewritten:
            return AIPublishGateResult(
                action="rewrite",
                markdown=rewritten,
                reason=(
                    "AI editorial gate supplied a rewritten draft "
                    f"({review.decision})."
                ),
                review=review,
            )
        return AIPublishGateResult(
            action="block",
            markdown=content_markdown,
            reason=(
                f"AI editorial gate returned {review.decision} without a "
                "rewritten draft."
            ),
            review=review,
        )
    return AIPublishGateResult(
        action="block",
        markdown=content_markdown,
        reason=f"AI editorial gate returned {review.decision}.",
        review=review,
    )


async def run_briefing_ai_review(
    settings: Settings,
    *,
    briefing_id: UUID,
    source: str = "auto_distribution",
) -> dict[str, Any]:
    """Run and persist one AI review for a distributed briefing."""
    config = get_ai_review_config(settings)
    if not settings.ai_review_auto_enabled:
        return {"status": "skipped", "reason": "auto_disabled"}
    if not config.enabled:
        return {"status": "skipped", "reason": "not_configured"}
    briefing_row = await get_briefing_review_context(briefing_id)
    if briefing_row is None:
        return {"status": "skipped", "reason": "briefing_not_found"}
    briefing = dict(briefing_row)
    if str(briefing.get("status") or "").strip().upper() != "DISTRIBUTED":
        return {"status": "skipped", "reason": "briefing_not_distributed"}
    if await has_ai_review(briefing_id=briefing_id, source=source):
        return {"status": "skipped", "reason": "already_reviewed"}

    review = await run_openai_editorial_review(
        settings,
        AIReviewInput(
            briefing_id=briefing_id,
            content_markdown=str(briefing.get("content_markdown") or ""),
            latest_run_model=(
                str(briefing.get("run_llm_model") or "").strip() or None
            ),
            latest_run_decision=(
                str(briefing.get("run_decision") or "").strip() or None
            ),
            rewrite_count=int(briefing.get("run_rewrite_count") or 0),
            selected_items_context=(
                list(briefing.get("run_selected_items") or [])
                if isinstance(briefing.get("run_selected_items"), list)
                else None
            ),
        ),
    )
    review_id = await insert_ai_review(
        briefing_id=briefing_id,
        run_id=briefing.get("run_id"),
        source=source,
        reviewer_provider=review.reviewer_provider,
        reviewer_model=review.reviewer_model,
        reasoning_effort=review.reasoning_effort,
        decision=review.decision,
        issue_tags=review.issue_tags,
        edited_markdown=review.edited_markdown,
        notes=review.notes,
        raw_response=review.raw_response,
    )
    return {
        "status": "stored",
        "review_id": str(review_id),
        "briefing_id": str(briefing_id),
        "decision": review.decision,
        "reviewer_model": review.reviewer_model,
    }


__all__ = [
    "AIReviewConfig",
    "AIReviewInput",
    "AIReviewResult",
    "AIPublishGateResult",
    "REVIEW_DECISIONS",
    "REVIEW_ISSUE_TAGS",
    "get_ai_review_config",
    "run_prepublish_ai_review_gate",
    "run_briefing_ai_review",
    "run_openai_editorial_review",
]
