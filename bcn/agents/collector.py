from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

import feedparser
import httpx
from typing_extensions import override

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import AgentSkill
from a2a.utils import new_agent_text_message

from bcn.config import Settings
from bcn.db import insert_news_item
from bcn.scraper import Scraper

logger = logging.getLogger(__name__)

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
        description="Collect tweets from security researchers via Apify",
        tags=["twitter", "apify"],
        examples=["collect twitter"],
    ),
    AgentSkill(
        id="collect_all",
        name="Collect All",
        description="Run all collectors concurrently",
        tags=["all"],
        examples=["collect all", "collect"],
    ),
]

# From n8n/collectors/ghsa_collector.json
GHSA_QUERY = """
query {
  securityAdvisories(first: 100, orderBy: {field: PUBLISHED_AT, direction: DESC}) {
    nodes {
      ghsaId
      summary
      description
      permalink
      severity
      publishedAt
      references { url }
      identifiers { type value }
    }
  }
}
"""


class CollectorExecutor(AgentExecutor):
    def __init__(self, settings: Settings):
        self.settings = settings
        self.scraper = Scraper(
            content_limit=settings.scrape_content_limit,
            min_content_length=settings.scrape_min_content_length,
        )
        self._http = httpx.AsyncClient(timeout=60)

    @override
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        msg = context.get_user_input() or "collect_all"
        text = msg.lower()

        if "ghsa" in text:
            count = await self._collect_ghsa()
            result = f"GHSA: collected {count} items"
        elif "rss" in text:
            count = await self._collect_rss()
            result = f"RSS: collected {count} items"
        elif "twitter" in text:
            count = await self._collect_twitter()
            result = f"Twitter: collected {count} items"
        else:
            counts = await self._collect_all()
            result = f"All: GHSA={counts[0]}, RSS={counts[1]}, Twitter={counts[2]}"

        logger.info(result)
        event_queue.enqueue_event(new_agent_text_message(result))

    @override
    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise Exception("cancel not supported")

    async def _collect_all(self) -> tuple[int, int, int]:
        results = await asyncio.gather(
            self._collect_ghsa(),
            self._collect_rss(),
            self._collect_twitter(),
            return_exceptions=True,
        )
        return tuple(r if isinstance(r, int) else 0 for r in results)

    # ------------------------------------------------------------------
    # GHSA - ported from n8n/collectors/ghsa_collector.json
    # ------------------------------------------------------------------
    async def _collect_ghsa(self) -> int:
        if not self.settings.github_token:
            logger.warning("No GitHub token configured, skipping GHSA collection")
            return 0

        resp = await self._http.post(
            "https://api.github.com/graphql",
            headers={
                "Authorization": f"Bearer {self.settings.github_token}",
                "User-Agent": "bcn-cloud-agent",
                "Content-Type": "application/json",
            },
            json={"query": GHSA_QUERY},
        )
        resp.raise_for_status()
        data = resp.json()

        nodes = (
            data.get("data", {})
            .get("securityAdvisories", {})
            .get("nodes", [])
        )

        keyword_patterns = [
            re.compile(kw, re.IGNORECASE) for kw in self.settings.ghsa_keywords
        ]
        allowed = set(self.settings.ghsa_severities)

        count = 0
        for item in nodes:
            if item.get("severity") not in allowed:
                continue

            text = f"{item.get('summary', '')} {item.get('description', '')}"
            if not any(p.search(text) for p in keyword_patterns):
                continue

            # Prefer external link (not github.com or nist.gov)
            refs = [r["url"] for r in item.get("references", [])]
            url = next(
                (u for u in refs if "github.com" not in u and "nist.gov" not in u),
                item.get("permalink", ""),
            )

            inserted = await insert_news_item(
                source_type="ghsa",
                source_id=item["ghsaId"],
                url=url,
                title=item.get("summary"),
                published_at=item.get("publishedAt", datetime.now(timezone.utc).isoformat()),
                raw_data=item,
            )
            if inserted:
                count += 1

        return count

    # ------------------------------------------------------------------
    # RSS - ported from n8n/collectors/rss_cisa.json
    # ------------------------------------------------------------------
    async def _collect_rss(self) -> int:
        count = 0
        for feed_url in self.settings.rss_feeds:
            try:
                resp = await self._http.get(feed_url)
                resp.raise_for_status()
                feed = feedparser.parse(resp.text)
            except Exception as exc:
                logger.warning("Failed to fetch RSS %s: %s", feed_url, exc)
                continue

            for entry in feed.entries:
                source_id = getattr(entry, "id", None) or getattr(entry, "link", "")
                url = getattr(entry, "link", "")
                title = getattr(entry, "title", "")
                published = getattr(entry, "published", None) or datetime.now(timezone.utc).isoformat()

                # Scrape full content for RSS items
                full_content = ""
                if url:
                    full_content = await self.scraper.scrape(url)

                inserted = await insert_news_item(
                    source_type="rss",
                    source_id=source_id,
                    url=url,
                    title=title,
                    published_at=published,
                    raw_data={
                        "feed_url": feed_url,
                        "title": title,
                        "link": url,
                        "published": published,
                        "summary": getattr(entry, "summary", ""),
                    },
                    full_content=full_content or None,
                )
                if inserted:
                    count += 1

        return count

    # ------------------------------------------------------------------
    # Twitter/X - ported from n8n/collectors/x_apify.json
    # ------------------------------------------------------------------
    async def _collect_twitter(self) -> int:
        if not self.settings.apify_token:
            logger.warning("No Apify token configured, skipping Twitter collection")
            return 0

        resp = await self._http.post(
            "https://api.apify.com/v2/acts/apidojo~twitter-scraper-lite/run-sync-get-dataset-items",
            headers={
                "Authorization": f"Bearer {self.settings.apify_token}",
                "Content-Type": "application/json",
            },
            json={
                "twitterHandles": self.settings.twitter_handles,
                "maxItems": self.settings.twitter_max_items,
            },
            timeout=120,
        )
        resp.raise_for_status()
        tweets = resp.json()

        count = 0
        for tweet in tweets:
            # Fallback chains from n8n/collectors/x_apify.json
            source_id = str(tweet.get("id") or tweet.get("conversationId") or "")
            if not source_id:
                continue

            url = tweet.get("url") or tweet.get("twitterUrl") or ""
            title = tweet.get("text") or tweet.get("fullText") or ""
            published = tweet.get("createdAt") or tweet.get("date") or datetime.now(timezone.utc).isoformat()

            inserted = await insert_news_item(
                source_type="twitter",
                source_id=source_id,
                url=url,
                title=title,
                published_at=published,
                raw_data=tweet,
            )
            if inserted:
                count += 1

        return count
