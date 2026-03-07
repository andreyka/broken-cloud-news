"""Analyst domain service shared by agent execution and replay lanes."""

from __future__ import annotations

import json
import logging
from typing import Protocol

from bcn.agents.analyst.llm import AnalystLLM
from bcn.common.config import Settings
from bcn.common.db import update_item_analyzed
from bcn.common.llm import LLMClient
from bcn.common.scraper import Scraper

logger = logging.getLogger(__name__)


class AnalystWorkflowProtocol(Protocol):
    """Protocol used by replay lanes for side-effect-free analyst access."""

    async def close(self) -> None:
        """Release any resources held by the analyst workflow service."""

    async def analyze_item_and_save(self, item: dict) -> None:
        """Analyze one item and persist the refreshed DB fields."""


class AnalystService:
    """Domain service for analyzing items and persisting results."""

    def __init__(
        self,
        settings: Settings,
        *,
        llm_client: LLMClient | None = None,
        scraper: Scraper | None = None,
    ) -> None:
        self.settings = settings
        self._owns_llm_client = llm_client is None
        self._owns_scraper = scraper is None
        self.llm_client = llm_client if llm_client is not None else LLMClient.from_settings(settings)
        self.analyst_llm = AnalystLLM(self.llm_client)
        self.scraper = scraper if scraper is not None else Scraper(
            content_limit=settings.scrape_content_limit,
            min_content_length=settings.scrape_min_content_length,
        )

    async def analyze_item_and_save(
        self,
        item: dict,
        *,
        update_item_fn=update_item_analyzed,
    ) -> None:
        """Analyze one item and persist the updated DB fields."""
        title: str = item["title"] or ""
        content: str = item["full_content"] or ""

        if not content and item["url"]:
            if item["source_type"] in ("rss", "ghsa"):
                content = await self.scraper.scrape(item["url"])

        if item["source_type"] == "ghsa":
            try:
                raw = (
                    json.loads(item["raw_data"])
                    if isinstance(item["raw_data"], str)
                    else item["raw_data"]
                )
                desc = raw.get("description", "")
                severity = raw.get("severity", "")
                if desc and (not content or len(content) < 200):
                    content = f"[Severity: {severity}]\n{desc}\n\n{content or ''}"
            except Exception:
                pass
        elif item["source_type"] in ("twitter", "reddit"):
            try:
                raw = (
                    json.loads(item["raw_data"])
                    if isinstance(item["raw_data"], str)
                    else item["raw_data"]
                )
                references = raw.get("references", [])
                for ref in references[:3]:
                    if isinstance(ref, dict) and ref.get("url"):
                        scraped_ref = await self.scraper.scrape(ref["url"])
                        if scraped_ref:
                            content += (
                                f"\n\n--- Scraped content from {ref['url']} ---\n"
                                f"{scraped_ref[:3000]}"
                            )
            except Exception as exc:
                logger.warning(
                    "Failed to scrape %s references for %s: %s",
                    item["source_type"],
                    item["id"],
                    exc,
                )

        if not content:
            content = title

        result = await self.analyst_llm.analyze_item(
            title,
            content,
            url=item["url"] or "",
        )
        await update_item_fn(
            item_id=item["id"],
            summary=result.summary,
            relevance_score=result.relevance_score,
            ai_tags=result.tags,
            full_content=(content if content != title else item["full_content"]),
            image_prompt=result.image_prompt,
            canonical_url=result.canonical_url,
        )
        logger.info(
            "Analyzed %s [%s] score=%d",
            item["source_id"],
            item["source_type"],
            result.relevance_score,
        )

    async def close(self) -> None:
        """Release resources owned by this analyst service."""
        if self._owns_scraper:
            await self.scraper.close()
        if self._owns_llm_client:
            await self.llm_client.close()


__all__ = [
    "AnalystService",
    "AnalystWorkflowProtocol",
]
