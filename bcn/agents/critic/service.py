"""Critic domain service shared by the control plane and transport adapters."""

from __future__ import annotations

import logging
from typing import Any

from bcn.agents.critic.llm import CriticLLM
from bcn.briefing.quality import BriefingQualityGate
from bcn.common.config import Settings
from bcn.common.llm import LLMClient
from bcn.contracts.review import CritiqueRequest
from bcn.contracts.review import parse_critique_request_payload
from bcn.contracts.review import render_critique_request_payload

logger = logging.getLogger(__name__)


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
