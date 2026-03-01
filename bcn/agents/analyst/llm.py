"""Analyst LLM interactions."""

import logging

from bcn.agents.tools import allow_tool_urls
from bcn.agents.analyst.prompt import ANALYZER_SYSTEM_PROMPT
from bcn.agents.tools import fetch_page_content
from bcn.common.llm import LLMClient
from bcn.common.models import AnalysisResult

logger = logging.getLogger(__name__)


class AnalystLLM:
    def __init__(self, client: LLMClient):
        self.client = client

    async def analyze_item(self, title: str, content: str, url: str) -> AnalysisResult:
        """Score and summarize a single news item."""
        user_msg = f"Title: {title}\nURL: {url}\n\nContent: {content}"
        allowed_urls = [url] if (url or "").strip() else []
        tools = [fetch_page_content] if allowed_urls else None
        with allow_tool_urls(allowed_urls):
            raw = await self.client.chat_for_role(
                role="analyst",
                system_prompt=ANALYZER_SYSTEM_PROMPT,
                user_content=user_msg,
                json_response=True,
                tools=tools,
            )

        try:
            parsed = self.client.parse_json_response(raw)
            return AnalysisResult(
                summary=parsed.get("summary", raw[:500]),
                relevance_score=max(1, min(10, int(parsed.get("relevance_score", 5)))),
                tags=parsed.get("tags", []),
                image_prompt=parsed.get("image_prompt", "cloud security concept art"),
                canonical_url=parsed.get("canonical_url"),
            )
        except Exception:
            logger.warning("Failed to parse LLM JSON, using fallback")
            return AnalysisResult(
                summary=raw[:500],
                relevance_score=5,
                tags=[],
                image_prompt="cloud security concept art",
                canonical_url=None,
            )
