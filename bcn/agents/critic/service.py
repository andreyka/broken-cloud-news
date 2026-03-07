"""Critic domain service shared by the control plane and legacy agent."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from typing import Any

from bcn.agents.critic.llm import CriticLLM
from bcn.briefing.quality import BriefingQualityGate
from bcn.common.config import Settings
from bcn.common.llm import LLMClient

logger = logging.getLogger(__name__)

_CRITIQUE_REQUEST_PREFIX = "critique_briefing::"


@dataclass(frozen=True)
class CritiqueRequest:
    """Explicit critic input prepared by the control plane."""

    draft_markdown: str
    items: tuple[dict[str, Any], ...] = ()
    mode: str = "standard"
    source: str = "input"
    recent_briefings: tuple[dict[str, Any], ...] = ()
    gate_hard_issues: tuple[str, ...] = ()
    gate_soft_issues: tuple[str, ...] = ()


def render_critique_request_payload(request: CritiqueRequest) -> str:
    """Render a structured critique request for agent transport."""
    payload = {
        "draft_markdown": request.draft_markdown,
        "gate_hard_issues": list(request.gate_hard_issues),
        "gate_soft_issues": list(request.gate_soft_issues),
        "items": list(request.items),
        "mode": str(request.mode or "standard"),
        "recent_briefings": list(request.recent_briefings),
        "source": str(request.source or "input"),
    }
    return _CRITIQUE_REQUEST_PREFIX + json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )


def parse_critique_request_payload(text: str) -> CritiqueRequest | None:
    """Parse a structured critique request from agent input text."""
    raw_text = str(text or "").strip()
    if not raw_text or raw_text.lower() == "critique_latest":
        return None
    if raw_text.startswith("critique_markdown::"):
        return CritiqueRequest(
            draft_markdown=raw_text.split("::", 1)[1].strip(),
        )
    if not raw_text.startswith(_CRITIQUE_REQUEST_PREFIX):
        return CritiqueRequest(draft_markdown=raw_text)

    raw_payload = raw_text[len(_CRITIQUE_REQUEST_PREFIX) :].strip()
    if not raw_payload:
        return None

    try:
        decoded = json.loads(raw_payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None

    draft_markdown = str(decoded.get("draft_markdown") or "").strip()
    if not draft_markdown:
        return None

    items_raw = decoded.get("items", [])
    recent_raw = decoded.get("recent_briefings", [])
    gate_hard_raw = decoded.get("gate_hard_issues", [])
    gate_soft_raw = decoded.get("gate_soft_issues", [])

    return CritiqueRequest(
        draft_markdown=draft_markdown,
        items=tuple(item for item in items_raw if isinstance(item, dict)),
        mode=str(decoded.get("mode") or "standard").strip() or "standard",
        source=str(decoded.get("source") or "input").strip() or "input",
        recent_briefings=tuple(item for item in recent_raw if isinstance(item, dict)),
        gate_hard_issues=tuple(str(item).strip() for item in gate_hard_raw if str(item).strip()),
        gate_soft_issues=tuple(str(item).strip() for item in gate_soft_raw if str(item).strip()),
    )


class CriticService:
    """Domain service for explicit briefing critique without DB ownership."""

    def __init__(
        self,
        settings: Settings,
        *,
        llm_client: LLMClient | None = None,
    ) -> None:
        self.settings = settings
        self._owns_llm_client = llm_client is None
        self.llm_client = (
            llm_client if llm_client is not None else LLMClient.from_settings(settings)
        )
        self.critic_llm = CriticLLM(self.llm_client)
        self.quality = BriefingQualityGate(settings)

    async def evaluate(self, request: CritiqueRequest) -> dict[str, Any]:
        """Critique one explicit briefing payload."""
        min_chars, _, hard_max_chars = self.quality.char_limits(request.mode)
        gate = self.quality.evaluate(
            markdown=request.draft_markdown,
            selected_items=list(request.items),
            mode=request.mode,
            min_chars=min_chars,
            hard_max_chars=hard_max_chars,
        )
        critique = await self.critic_llm.critique_briefing(
            draft_markdown=request.draft_markdown,
            items=list(request.items),
            mode=request.mode,
            gate_hard_issues=list(request.gate_hard_issues)
            or [str(item) for item in gate.get("hard_issues", [])],
            gate_soft_issues=list(request.gate_soft_issues)
            or [str(item) for item in gate.get("soft_issues", [])],
            recent_briefings=list(request.recent_briefings),
        )
        threshold_passed = self._passes_thresholds(critique)
        result = {
            "source": request.source,
            "gate_passed": bool(gate.get("passed", False)),
            "critic_passed": bool(critique.get("passed", False)),
            "critic_score": int(critique.get("score", 0) or 0),
            "critic_dimension_scores": critique.get("dimension_scores", {}),
            "threshold_passed": threshold_passed,
            "thresholds": {
                "min_score": int(self.settings.briefing_critic_min_score),
                "min_actionability": int(
                    self.settings.briefing_critic_min_actionability
                ),
                "min_source_diversity": int(
                    self.settings.briefing_critic_min_source_diversity
                ),
                "min_link_hygiene": int(self.settings.briefing_critic_min_link_hygiene),
            },
            "gate_issues": [str(item) for item in gate.get("issues", [])],
            "critic_issues": [str(item) for item in critique.get("issues", [])],
            "recommendations": [
                str(item) for item in critique.get("recommendations", [])
            ],
        }
        logger.info(
            "Critique done for %s: gate=%s critic=%s score=%s",
            request.source,
            result["gate_passed"],
            result["critic_passed"],
            result["critic_score"],
        )
        return result

    def _passes_thresholds(self, critique: dict[str, object]) -> bool:
        """Return whether critique output meets configured release thresholds."""
        if not bool(critique.get("passed", False)):
            return False
        score = int(critique.get("score", 0) or 0)
        dims = critique.get("dimension_scores", {}) or {}
        if not isinstance(dims, dict):
            dims = {}
        actionability = int(dims.get("actionability", 0) or 0)
        link_hygiene = int(dims.get("link_hygiene", 0) or 0)
        return (
            score >= int(self.settings.briefing_critic_min_score)
            and actionability >= int(self.settings.briefing_critic_min_actionability)
            and link_hygiene >= int(self.settings.briefing_critic_min_link_hygiene)
        )

    async def close(self) -> None:
        """Release resources owned by this critic service."""
        if self._owns_llm_client:
            await self.llm_client.close()


__all__ = [
    "CriticService",
    "CritiqueRequest",
    "parse_critique_request_payload",
    "render_critique_request_payload",
]
