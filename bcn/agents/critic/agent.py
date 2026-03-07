"""Legacy critic agent wrapper over the explicit critic service."""

from __future__ import annotations

import json

from a2a.server.agent_execution import AgentExecutor
from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from a2a.types import AgentSkill
from a2a.utils import new_agent_text_message
from typing_extensions import override

from bcn.agents.base import enqueue_event_safe
from bcn.agents.critic.service import CriticService
from bcn.agents.critic.service import parse_critique_request_payload
from bcn.common.config import Settings

SKILLS = [
    AgentSkill(
        id="critique_briefing",
        name="Critique Briefing",
        description="Critique explicit briefing payloads provided by the control plane",
        tags=["briefing", "critic", "quality"],
        examples=[
            "critique_briefing::{...json...}",
            "critique_markdown::<text>",
        ],
    ),
]


class CriticExecutor(AgentExecutor):
    """A2A worker that critiques explicit briefing payloads."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._service = CriticService(settings)

    async def close(self) -> None:
        """Release critic resources."""
        await self._service.close()

    @staticmethod
    def _legacy_boundary_message() -> str:
        """Return a clear message for callers still using implicit latest lookup."""
        return (
            "Critic requires an explicit briefing payload from the control plane; "
            "legacy latest-briefing lookup is no longer supported."
        )

    @override
    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Critique provided explicit briefing payload."""
        request = parse_critique_request_payload(context.get_user_input() or "")
        if request is None:
            await enqueue_event_safe(
                event_queue,
                new_agent_text_message(self._legacy_boundary_message()),
            )
            return

        result = await self._service.evaluate(request)
        await enqueue_event_safe(
            event_queue,
            new_agent_text_message(json.dumps(result, ensure_ascii=False, indent=2)),
        )

    @override
    async def cancel(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Cancel is not supported."""
        raise NotImplementedError("cancel not supported")
