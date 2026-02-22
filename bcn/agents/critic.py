"""Critic agent: evaluates briefing quality and returns structured recommendations."""

from __future__ import annotations

import json
import logging

from typing_extensions import override

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import AgentSkill
from a2a.utils import new_agent_text_message

from bcn.briefing.quality import BriefingQualityGate
from bcn.config import Settings
from bcn.db import get_items_by_ids, get_latest_any_briefing
from bcn.llm import LLMClient

logger = logging.getLogger(__name__)

SKILLS = [
    AgentSkill(
        id="critique_briefing",
        name="Critique Briefing",
        description="Critique briefing markdown and return quality assessment",
        tags=["briefing", "critic", "quality"],
        examples=["critique_latest", "critique_markdown::<text>"],
    ),
]


class CriticExecutor(AgentExecutor):
    """A2A agent that critiques briefing quality using deterministic + LLM checks."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.llm = LLMClient(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            timeout=settings.llm_timeout,
        )
        self.quality = BriefingQualityGate(settings)

    @override
    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Critique provided markdown or the latest briefing."""
        raw = (context.get_user_input() or "").strip()
        source = "input"
        draft_markdown = ""
        items: list[dict] = []

        if not raw or raw.lower() == "critique_latest":
            briefing = await get_latest_any_briefing()
            if not briefing:
                event_queue.enqueue_event(new_agent_text_message("No briefing found to critique"))
                return
            source = f"briefing:{briefing['id']}"
            draft_markdown = str(briefing.get("content_markdown") or "")
            item_ids = list(briefing.get("item_ids") or [])
            if item_ids:
                items = [dict(r) for r in await get_items_by_ids(item_ids)]
        elif raw.startswith("critique_markdown::"):
            draft_markdown = raw.split("::", 1)[1].strip()
        else:
            draft_markdown = raw

        if not draft_markdown:
            event_queue.enqueue_event(new_agent_text_message("No markdown provided for critique"))
            return

        mode = "standard"
        min_chars, _, hard_max_chars = self.quality.char_limits(mode)
        gate = self.quality.evaluate(
            markdown=draft_markdown,
            selected_items=items,
            mode=mode,
            min_chars=min_chars,
            hard_max_chars=hard_max_chars,
        )

        critique = await self.llm.critique_briefing(
            draft_markdown=draft_markdown,
            items=items,
            mode=mode,
            gate_hard_issues=[str(i) for i in gate.get("hard_issues", [])],
            gate_soft_issues=[str(i) for i in gate.get("soft_issues", [])],
        )
        threshold_passed = self._passes_thresholds(critique)

        response = {
            "source": source,
            "gate_passed": bool(gate.get("passed", False)),
            "critic_passed": bool(critique.get("passed", False)),
            "critic_score": int(critique.get("score", 0) or 0),
            "critic_dimension_scores": critique.get("dimension_scores", {}),
            "threshold_passed": threshold_passed,
            "thresholds": {
                "min_score": int(self.settings.briefing_critic_min_score),
                "min_actionability": int(self.settings.briefing_critic_min_actionability),
                "min_source_diversity": int(self.settings.briefing_critic_min_source_diversity),
                "min_link_hygiene": int(self.settings.briefing_critic_min_link_hygiene),
            },
            "gate_issues": [str(i) for i in gate.get("issues", [])],
            "critic_issues": [str(i) for i in critique.get("issues", [])],
            "recommendations": [str(i) for i in critique.get("recommendations", [])],
        }
        logger.info(
            "Critique done for %s: gate=%s critic=%s score=%s",
            source,
            response["gate_passed"],
            response["critic_passed"],
            response["critic_score"],
        )
        event_queue.enqueue_event(
            new_agent_text_message(json.dumps(response, ensure_ascii=False, indent=2))
        )

    @override
    async def cancel(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Cancel is not supported."""
        raise NotImplementedError("cancel not supported")

    def _passes_thresholds(self, critique: dict[str, object]) -> bool:
        if not bool(critique.get("passed", False)):
            return False
        score = int(critique.get("score", 0) or 0)
        dims = critique.get("dimension_scores", {}) or {}
        if not isinstance(dims, dict):
            dims = {}
        actionability = int(dims.get("actionability", 0) or 0)
        source_diversity = int(dims.get("source_diversity", 0) or 0)
        link_hygiene = int(dims.get("link_hygiene", 0) or 0)
        return (
            score >= int(self.settings.briefing_critic_min_score)
            and actionability >= int(self.settings.briefing_critic_min_actionability)
            and source_diversity >= int(self.settings.briefing_critic_min_source_diversity)
            and link_hygiene >= int(self.settings.briefing_critic_min_link_hygiene)
        )
