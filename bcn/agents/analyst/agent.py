"""Legacy analyst agent wrapper over the control-plane analysis service."""

from __future__ import annotations

from a2a.server.agent_execution import AgentExecutor
from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from a2a.types import AgentSkill
from a2a.utils import new_agent_text_message
from typing_extensions import override

from bcn.agents.analyst.service import AnalystService
from bcn.agents.base import enqueue_event_safe
from bcn.common.config import Settings
from bcn.workflows.analysis import execute_analysis

SKILLS = [
    AgentSkill(
        id="analyze_new_items",
        name="Analyze New Items",
        description="Analyze unprocessed news items using Qwen LLM for relevance scoring",
        tags=["analysis", "llm"],
        examples=["analyze", "analyze_new_items"],
    ),
]


class AnalystExecutor(AgentExecutor):
    """A2A agent that scores and summarizes news items via the LLM."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.service = AnalystService(settings)
        self.llm_client = self.service.llm_client
        self.analyst_llm = self.service.analyst_llm
        self.scraper = self.service.scraper

    @override
    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Run the legacy analyst agent through the control-plane service."""
        del context

        message = await execute_analysis(
            self.settings,
            analyst_service=self.service,
            source="analyst_agent",
            manage_pool=False,
        )
        await enqueue_event_safe(event_queue, new_agent_text_message(message))

    async def close(self) -> None:
        """Release analyst resources."""
        await self.service.close()

    @override
    async def cancel(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Cancel is not supported."""
        raise NotImplementedError("cancel not supported")
