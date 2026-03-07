"""Legacy verifier agent wrapper over the explicit verifier service."""

from __future__ import annotations

import json

from a2a.server.agent_execution import AgentExecutor
from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from a2a.types import AgentSkill
from a2a.utils import new_agent_text_message
from typing_extensions import override

from bcn.agents.base import enqueue_event_safe
from bcn.agents.verifier.service import VerifierService
from bcn.agents.verifier.service import parse_verification_request_payload
from bcn.common.config import Settings

SKILLS = [
    AgentSkill(
        id="verify_briefing",
        name="Verify Briefing",
        description="Verify explicit briefing payloads provided by the control plane",
        tags=["briefing", "verifier", "factual"],
        examples=[
            "verify_briefing::{...json...}",
            "verify_markdown::<text>",
        ],
    ),
]


class VerifierExecutor(AgentExecutor):
    """A2A worker that verifies explicit briefing payloads."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._service = VerifierService(settings)

    async def close(self) -> None:
        """Release verifier resources."""
        await self._service.close()

    @staticmethod
    def _legacy_boundary_message() -> str:
        """Return a clear message for callers still using implicit latest lookup."""
        return (
            "Verifier requires an explicit briefing payload from the control plane; "
            "legacy latest-briefing lookup is no longer supported."
        )

    @override
    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Verify provided explicit briefing payload."""
        request = parse_verification_request_payload(context.get_user_input() or "")
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
