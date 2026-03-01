"""Verifier agent: validates factual integrity and link quality of briefings."""

from __future__ import annotations

import json
import logging

from a2a.server.agent_execution import AgentExecutor
from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from a2a.types import AgentSkill
from a2a.utils import new_agent_text_message
from typing_extensions import override

from bcn.agents.base import enqueue_event_safe
from bcn.briefing.verifier import BriefingFactVerifier
from bcn.common.config import Settings
from bcn.common.db import get_items_by_ids
from bcn.common.db import get_latest_any_briefing

logger = logging.getLogger(__name__)

SKILLS = [
    AgentSkill(
        id="verify_briefing",
        name="Verify Briefing",
        description=
        "Verify briefing facts, top-story quality, and link liveness",
        tags=["briefing", "verifier", "factual"],
        examples=["verify_latest", "verify_markdown::<text>"],
    ),
]


class VerifierExecutor(AgentExecutor):
    """A2A agent that verifies factual integrity of briefing drafts."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.verifier = BriefingFactVerifier(settings)

    async def close(self) -> None:
        """Release verifier resources."""
        await self.verifier.close()

    @override
    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Verify provided markdown or latest stored briefing."""
        raw = (context.get_user_input() or "").strip()
        source = "input"
        draft_markdown = ""
        items: list[dict] = []

        if not raw or raw.lower() == "verify_latest":
            briefing = await get_latest_any_briefing()
            if not briefing:
                await enqueue_event_safe(
                    event_queue,
                    new_agent_text_message("No briefing found to verify"),
                )
                return
            source = f"briefing:{briefing['id']}"
            draft_markdown = str(briefing.get("content_markdown") or "")
            item_ids = list(briefing.get("item_ids") or [])
            if item_ids:
                items = [dict(r) for r in await get_items_by_ids(item_ids)]
        elif raw.startswith("verify_markdown::"):
            draft_markdown = raw.split("::", 1)[1].strip()
        else:
            draft_markdown = raw

        if not draft_markdown:
            await enqueue_event_safe(
                event_queue,
                new_agent_text_message("No markdown provided for verification"),
            )
            return

        report = await self.verifier.evaluate(
            draft_markdown,
            items,
            mode="standard",
        )
        response = {
            "source": source,
            "verifier_passed": bool(report.get("passed", False)),
            "verifier_score": int(report.get("score", 0) or 0),
            "issues": [str(i) for i in report.get("issues", [])],
            "recommendations": [
                str(i) for i in report.get("recommendations", [])
            ],
            "dead_urls": [str(i) for i in report.get("dead_urls", [])],
            "top_story_ok": bool(report.get("top_story_ok", True)),
        }
        logger.info(
            "Verifier done for %s: passed=%s score=%s",
            source,
            response["verifier_passed"],
            response["verifier_score"],
        )
        await enqueue_event_safe(
            event_queue,
            new_agent_text_message(
                json.dumps(response, ensure_ascii=False, indent=2)),
        )

    @override
    async def cancel(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Cancel is not supported."""
        raise NotImplementedError("cancel not supported")
