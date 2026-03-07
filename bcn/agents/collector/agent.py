"""Legacy collector agent wrapper over the control-plane collection service."""

from __future__ import annotations

from a2a.server.agent_execution import AgentExecutor
from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from a2a.types import AgentSkill
from a2a.utils import new_agent_text_message
from typing_extensions import override

from bcn.agents.base import enqueue_event_safe
from bcn.agents.collector.service import CollectorService
from bcn.common.config import Settings
from bcn.workflows.collection import execute_collection

SKILLS = [
    AgentSkill(
        id="collect_ghsa",
        name="Collect GHSA",
        description="Collect GitHub Security Advisories (CRITICAL/HIGH, cloud-related)",
        tags=["ghsa", "github"],
        examples=["collect ghsa"],
    ),
    AgentSkill(
        id="collect_rss",
        name="Collect RSS",
        description="Collect from CISA and AWS Security Blog RSS feeds",
        tags=["rss", "cisa", "aws"],
        examples=["collect rss"],
    ),
    AgentSkill(
        id="collect_twitter",
        name="Collect Twitter",
        description="Collect tweets from security researchers via X API",
        tags=["twitter", "x"],
        examples=["collect twitter"],
    ),
    AgentSkill(
        id="collect_reddit",
        name="Collect Reddit",
        description="Collect top items from cloud security subreddits via RSS",
        tags=["reddit", "netsec", "subreddit"],
        examples=["collect reddit"],
    ),
    AgentSkill(
        id="collect_all",
        name="Collect All",
        description="Run all collectors concurrently",
        tags=["all"],
        examples=["collect all", "collect"],
    ),
]


class CollectorExecutor(AgentExecutor):
    """A2A wrapper that delegates collection to the control plane."""

    _clean_summary = staticmethod(CollectorService._clean_summary)
    _extract_tweet_reference_urls = staticmethod(
        CollectorService._extract_tweet_reference_urls
    )
    _build_tweet_full_content = staticmethod(CollectorService._build_tweet_full_content)
    _select_reddit_primary_url = staticmethod(
        CollectorService._select_reddit_primary_url
    )
    _is_internal_reddit_url = staticmethod(CollectorService._is_internal_reddit_url)
    _build_reddit_full_content = staticmethod(
        CollectorService._build_reddit_full_content
    )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.service = CollectorService(settings)
        self.scraper = self.service.scraper
        self._http = self.service._http

    @staticmethod
    def _resolve_collection_source(text: str) -> str:
        """Map a legacy collector skill/request string to one collection source."""
        normalized = str(text or "").strip().lower()
        if "ghsa" in normalized:
            return "ghsa"
        if "rss" in normalized:
            return "rss"
        if "twitter" in normalized:
            return "twitter"
        if "reddit" in normalized:
            return "reddit"
        return "all"

    @override
    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Run the legacy collector agent through the control-plane service."""
        source = self._resolve_collection_source(context.get_user_input() or "collect_all")
        message = await execute_collection(
            self.settings,
            source=source,
            collector_service=self.service,
            origin="collector_agent",
            manage_pool=False,
        )
        await enqueue_event_safe(event_queue, new_agent_text_message(message))

    async def close(self) -> None:
        """Release collector resources."""
        await self.service.close()

    @override
    async def cancel(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Cancel is not supported."""
        raise NotImplementedError("cancel not supported")
