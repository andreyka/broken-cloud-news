"""Legacy distributor agent wrapper over the plain distributor service."""

from __future__ import annotations

import logging
from uuid import UUID

from a2a.server.agent_execution import AgentExecutor
from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from a2a.types import AgentSkill
from a2a.utils import new_agent_text_message
from typing_extensions import override

from bcn.agents.base import enqueue_event_safe
from bcn.agents.distributor.service import DistributorService
from bcn.agents.distributor.service import _distribution_redaction_secrets
from bcn.agents.distributor.service import normalize_distribution_mode
from bcn.agents.distributor.service import parse_delivery_request_payload
from bcn.agents.distributor.service import render_delivery_result_payload
from bcn.common.config import Settings
from bcn.workflows.modes import ALL_MODES
from bcn.workflows.modes.common import extract_briefing_id

logger = logging.getLogger(__name__)
_SUPPORTED_MODES = frozenset(ALL_MODES)

SKILLS = [
    AgentSkill(
        id="deliver_briefing",
        name="Deliver Briefing",
        description="Deliver an explicit briefing payload provided by the control plane",
        tags=["deliver", "publish"],
        examples=["deliver_briefing::{...json...}"],
    ),
    AgentSkill(
        id="distribute_briefing",
        name="Distribute Briefing (Legacy)",
        description="Legacy alias retained for compatibility; control-plane callers should send explicit delivery payloads",
        tags=["legacy", "distribute"],
        examples=["distribute_briefing::<uuid>::regular_daily_briefing"],
    ),
]


class DistributorExecutor(AgentExecutor):
    """A2A worker that delivers explicit briefing payloads."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._service = DistributorService(settings)

    async def close(self) -> None:
        """Release resources owned by the shared distributor service."""
        await self._service.close()

    @override
    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Deliver one explicit briefing payload prepared by the control plane."""
        user_input = context.get_user_input() or ""
        request = parse_delivery_request_payload(user_input)
        if request is None:
            await enqueue_event_safe(
                event_queue,
                new_agent_text_message(
                    self._legacy_boundary_message(
                        requested_briefing_id=self._extract_requested_briefing_id(
                            user_input
                        ),
                        requested_mode=self._extract_requested_mode(user_input),
                    )
                ),
            )
            return

        result = await self._service.deliver(request)
        logger.info(result.message)
        await enqueue_event_safe(
            event_queue,
            new_agent_text_message(
                f"{render_delivery_result_payload(result)}\n{result.message}"
            ),
        )

    @staticmethod
    def _extract_requested_briefing_id(text: str) -> UUID | None:
        """Extract optional target briefing UUID from distributor skill text."""
        return extract_briefing_id(text)

    @staticmethod
    def _extract_requested_mode(text: str) -> str | None:
        """Extract optional distribution mode from skill text tokens."""
        for token in str(text or "").split("::"):
            candidate = token.strip().lower()
            if candidate.startswith("mode="):
                candidate = candidate.split("=", 1)[1].strip().lower()
            if candidate in _SUPPORTED_MODES:
                return candidate
        return None

    @staticmethod
    def _legacy_boundary_message(
        *,
        requested_briefing_id: UUID | None,
        requested_mode: str | None,
    ) -> str:
        """Return a clear message for legacy callers using the old contract."""
        mode = normalize_distribution_mode(requested_mode)
        target = (
            f" briefing_id={requested_briefing_id}"
            if requested_briefing_id is not None
            else ""
        )
        return (
            "Distributor requires an explicit delivery payload from the control plane; "
            f"legacy claim-and-distribute requests are no longer supported.{target} "
            f"mode={mode}"
        ).strip()

    @override
    async def cancel(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Cancel is not supported."""
        raise NotImplementedError("cancel not supported")
