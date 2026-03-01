"""Analyst agent: scores and summarizes news items using the Qwen LLM."""

from __future__ import annotations

import json
import logging

from a2a.server.agent_execution import AgentExecutor
from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from a2a.types import AgentSkill
from a2a.utils import new_agent_text_message
from typing_extensions import override

from bcn.agents.analyst.llm import AnalystLLM
from bcn.agents.base import enqueue_event_safe
from bcn.common.config import Settings
from bcn.common.db import get_new_items
from bcn.common.db import update_item_analyzed
from bcn.common.llm import LLMClient
from bcn.common.scraper import Scraper

logger = logging.getLogger(__name__)

SKILLS = [
    AgentSkill(
        id="analyze_new_items",
        name="Analyze New Items",
        description=
        "Analyze unprocessed news items using Qwen LLM for relevance scoring",
        tags=["analysis", "llm"],
        examples=["analyze", "analyze_new_items"],
    ),
]


class AnalystExecutor(AgentExecutor):
    """A2A agent that scores and summarizes news items via the LLM."""

    def __init__(self, settings: Settings) -> None:
        self.llm_client = LLMClient.from_settings(settings)
        self.analyst_llm = AnalystLLM(self.llm_client)
        self.scraper = Scraper(
            content_limit=settings.scrape_content_limit,
            min_content_length=settings.scrape_min_content_length,
        )

    @override
    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Analyze all ``NEW`` items: scrape if needed, then score via LLM."""
        items = await get_new_items()
        if not items:
            await enqueue_event_safe(
                event_queue, new_agent_text_message("No new items to analyze"))
            return

        analyzed = 0
        try:
            for item in items:
                await self._analyze_item_and_save(item)
                analyzed += 1
        finally:
            await self.scraper.close()

        msg = f"Analyzed {analyzed}/{len(items)} items"
        logger.info(msg)
        await enqueue_event_safe(event_queue, new_agent_text_message(msg))

    async def _analyze_item_and_save(self, item: dict) -> None:
        title: str = item["title"] or ""
        content: str = item["full_content"] or ""

        if not content and item["url"]:
            if item["source_type"] in ("rss", "ghsa"):
                content = await self.scraper.scrape(item["url"])

        if item["source_type"] == "ghsa":
            try:
                raw = (json.loads(item["raw_data"]) if isinstance(
                    item["raw_data"], str) else item["raw_data"])
                desc = raw.get("description", "")
                severity = raw.get("severity", "")
                if desc and (not content or len(content) < 200):
                    content = f"[Severity: {severity}]\n{desc}\n\n{content or ''}"
            except Exception:
                pass
        elif item["source_type"] == "twitter":
            try:
                raw = (json.loads(item["raw_data"]) if isinstance(
                    item["raw_data"], str) else item["raw_data"])
                references = raw.get("references", [])
                for ref in references:
                    if isinstance(ref, dict) and ref.get("url"):
                        scraped_ref = await self.scraper.scrape(ref["url"])
                        if scraped_ref:
                            content += f"\n\n--- Scraped content from {ref['url']} ---\n{scraped_ref[:3000]}"
            except Exception as exc:
                logger.warning("Failed to scrape tweet references for %s: %s",
                               item["id"], exc)

        if not content:
            content = title

        try:
            result = await self.analyst_llm.analyze_item(title,
                                                         content,
                                                         url=item["url"] or "")
            await update_item_analyzed(
                item_id=item["id"],
                summary=result.summary,
                relevance_score=result.relevance_score,
                ai_tags=result.tags,
                full_content=(content
                              if content != title else item["full_content"]),
                image_prompt=result.image_prompt,
                canonical_url=result.canonical_url,
            )
            logger.info(
                "Analyzed %s [%s] score=%d",
                item["source_id"],
                item["source_type"],
                result.relevance_score,
            )
        except Exception:
            logger.exception("Failed to analyze item %s", item["id"])

    @override
    async def cancel(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Cancel is not supported."""
        raise NotImplementedError("cancel not supported")
