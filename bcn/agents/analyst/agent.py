"""Analyst agent: scores and summarizes news items using the Qwen LLM."""

from __future__ import annotations

import logging
from uuid import UUID

from a2a.server.agent_execution import AgentExecutor
from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from a2a.types import AgentSkill
from a2a.utils import new_agent_text_message
from typing_extensions import override

from bcn.agents.analyst.service import AnalystService
from bcn.agents.base import enqueue_event_safe
from bcn.common.config import Settings
from bcn.common.db import get_new_items
from bcn.common.db import release_items_from_analyzing
from bcn.common.db import update_item_analyzed

logger = logging.getLogger(__name__)

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
        """Analyze all ``NEW`` items: scrape if needed, then score via LLM."""
        items = await get_new_items(
            stale_analyzing_minutes=self.settings.analysis_retry_stale_analyzing_minutes,
            max_analysis_retries=self.settings.analysis_retry_max_attempts,
        )
        if not items:
            await enqueue_event_safe(
                event_queue, new_agent_text_message("No new items to analyze")
            )
            return

        analyzed = 0
        failed = 0
        for item in items:
            try:
                await self._analyze_item_and_save(item)
                analyzed += 1
            except Exception as exc:
                logger.exception("Failed to analyze item %s", item.get("id"))
                if str(item.get("status", "")).upper() == "ANALYZING":
                    item_id = self._coerce_uuid(item.get("id"))
                    if item_id:
                        try:
                            await release_items_from_analyzing(
                                [item_id],
                                error=f"{type(exc).__name__}: {exc}",
                                max_retries=self.settings.analysis_retry_max_attempts,
                                base_delay_seconds=self.settings.analysis_retry_base_delay_seconds,
                                max_delay_seconds=self.settings.analysis_retry_max_delay_seconds,
                            )
                        except Exception:
                            logger.exception(
                                "Failed to release ANALYZING item %s after analysis error",
                                item_id,
                            )
                failed += 1

        msg = f"Analyzed {analyzed}/{len(items)} items"
        if failed:
            msg += f" ({failed} failed)"
        logger.info(msg)
        await enqueue_event_safe(event_queue, new_agent_text_message(msg))

    async def _analyze_item_and_save(self, item: dict) -> None:
        await self.service.analyze_item_and_save(
            item,
            update_item_fn=update_item_analyzed,
        )

    @staticmethod
    def _coerce_uuid(value: object) -> UUID | None:
        if isinstance(value, UUID):
            return value
        try:
            return UUID(str(value))
        except Exception:
            return None

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
