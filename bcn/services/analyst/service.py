"""Analyst domain service shared by live execution and replay lanes."""

from __future__ import annotations

import json
import logging
from typing import Protocol

from bcn.services.analyst.llm import AnalystLLM
from bcn.common.config import Settings
from bcn.common.llm import LLMClient
from bcn.common.models import AnalyzedItemUpdate
from bcn.common.scraper import Scraper

logger = logging.getLogger(__name__)


class AnalystWorkflowProtocol(Protocol):
    """Protocol used by replay lanes for side-effect-free analyst access."""

    async def close(self) -> None:
        """Release any resources held by the analyst workflow service."""

    async def analyze_item(self, item: dict) -> AnalyzedItemUpdate:
        """Analyze one item and return the DB-ready update payload."""


class AnalystService:
    """Domain service for item analysis without workflow DB ownership."""

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

    async def _build_analysis_content(self, item: dict) -> str | None:
        """Collect the best available content for one item before LLM analysis."""
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

        return content or None

    async def analyze_item(self, item: dict) -> AnalyzedItemUpdate:
        """Analyze one item and return the updated DB fields."""
        title: str = item["title"] or ""
        content = await self._build_analysis_content(item)
        if not content:
            content = title

        result = await self.analyst_llm.analyze_item(
            title,
            content,
            url=item["url"] or "",
        )
        update = AnalyzedItemUpdate(
            summary=result.summary,
            relevance_score=result.relevance_score,
            ai_tags=list(result.tags),
            full_content=(content if content != title else item["full_content"]),
            image_prompt=result.image_prompt,
            canonical_url=result.canonical_url,
        )
        logger.info(
            "Analyzed %s [%s] score=%d",
            item["source_id"],
            item["source_type"],
            update.relevance_score,
        )
        return update

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
