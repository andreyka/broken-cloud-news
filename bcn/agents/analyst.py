from __future__ import annotations

import logging

from typing_extensions import override

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import AgentSkill
from a2a.utils import new_agent_text_message

from bcn.config import Settings
from bcn.db import get_new_items, update_item_analyzed
from bcn.llm import LLMClient
from bcn.scraper import Scraper

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
    def __init__(self, settings: Settings):
        self.llm = LLMClient(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            timeout=settings.llm_timeout,
        )
        self.scraper = Scraper(
            content_limit=settings.scrape_content_limit,
            min_content_length=settings.scrape_min_content_length,
        )

    @override
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        items = await get_new_items()
        if not items:
            event_queue.enqueue_event(new_agent_text_message("No new items to analyze"))
            return

        analyzed = 0
        for item in items:
            title = item["title"] or ""
            content = item["full_content"] or ""

            # If no full content and source is RSS, attempt scraping
            # (collector may have already done this, but handle the gap)
            if not content and item["source_type"] == "rss" and item["url"]:
                content = await self.scraper.scrape(item["url"])

            # For non-RSS items without full_content, use title as content
            if not content:
                content = title

            try:
                result = await self.llm.analyze_item(title, content)
                await update_item_analyzed(
                    item_id=item["id"],
                    summary=result.summary,
                    juiciness_score=result.juiciness_score,
                    ai_tags=result.tags,
                    full_content=content if content != title else item["full_content"],
                    image_prompt=result.image_prompt,
                )
                analyzed += 1
                logger.info(
                    "Analyzed %s [%s] score=%d",
                    item["source_id"],
                    item["source_type"],
                    result.juiciness_score,
                )
            except Exception:
                logger.exception("Failed to analyze item %s", item["id"])
                continue

        msg = f"Analyzed {analyzed}/{len(items)} items"
        logger.info(msg)
        event_queue.enqueue_event(new_agent_text_message(msg))

    @override
    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise Exception("cancel not supported")
