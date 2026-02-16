"""Collector agent: fetches news from GHSA, RSS feeds, and Twitter/X."""

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
        description="Collect tweets from security researchers via X API",
        tags=["twitter", "x"],
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

# GHSA GraphQL query
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

_SKIP_SCRAPE_DOMAINS = frozenset({
    "nvd.nist.gov", "cve.mitre.org", "cve.org", "access.redhat.com/errata",
})


class CollectorExecutor(AgentExecutor):
    """A2A agent that collects cloud security news from multiple sources."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.scraper = Scraper(
            content_limit=settings.scrape_content_limit,
            min_content_length=settings.scrape_min_content_length,
        )
        self._http = httpx.AsyncClient(timeout=60)

    @override
    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Dispatch to the appropriate collector based on the user message."""
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
    async def cancel(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Cancel is not supported."""
        raise NotImplementedError("cancel not supported")

    async def _collect_all(self) -> tuple[int, int, int]:
        """Run all collectors concurrently and return per-source counts."""
        results = await asyncio.gather(
            self._collect_ghsa(),
            self._collect_rss(),
            self._collect_twitter(),
            return_exceptions=True,
        )
        return tuple(r if isinstance(r, int) else 0 for r in results)

    # ------------------------------------------------------------------
    # GHSA collection
    # ------------------------------------------------------------------

    async def _collect_ghsa(self) -> int:
        """Fetch GitHub Security Advisories matching cloud keywords.

        Returns:
            Number of newly inserted items.
        """
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

        nodes: list[dict] = (
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

            refs = [r["url"] for r in item.get("references", [])]

            url = next(
                (u for u in refs if "github.com" not in u and "nist.gov" not in u),
                item.get("permalink", ""),
            )

            full_content = await self._enrich_ghsa_content(item, refs)

            inserted = await insert_news_item(
                source_type="ghsa",
                source_id=item["ghsaId"],
                url=url,
                title=item.get("summary"),
                published_at=item.get(
                    "publishedAt", datetime.now(timezone.utc).isoformat()
                ),
                raw_data=item,
                full_content=full_content or None,
            )
            if inserted:
                count += 1

        return count

    async def _enrich_ghsa_content(
        self,
        item: dict,
        refs: list[str],
    ) -> str:
        """Build enriched content for a GHSA item by scraping reference links.

        Prioritizes blog posts, GitHub repo descriptions, and PoC repos.
        Falls back to the advisory description if scraping yields nothing.

        Args:
            item: The raw GHSA advisory node.
            refs: Reference URLs from the advisory.

        Returns:
            Concatenated content sections separated by ``---``.
        """
        parts: list[str] = []

        description = item.get("description", "")
        if description:
            parts.append(f"[Advisory Description]\n{description}")

        cves = [
            ident["value"]
            for ident in item.get("identifiers", [])
            if ident.get("type") == "CVE"
        ]
        if cves:
            parts.append(f"[CVE IDs] {', '.join(cves)}")

        parts.append(f"[Severity] {item.get('severity', 'UNKNOWN')}")

        github_refs: list[str] = []
        other_refs: list[str] = []
        for ref_url in refs:
            if any(d in ref_url for d in _SKIP_SCRAPE_DOMAINS):
                continue
            if "github.com" in ref_url:
                github_refs.append(ref_url)
            else:
                other_refs.append(ref_url)

        scrape_targets = github_refs[:2] + other_refs[:1]

        for ref_url in scrape_targets:
            try:
                scraped = await self.scraper.scrape(ref_url)
                if scraped and len(scraped) >= self.scraper.min_content_length:
                    label = "GitHub" if "github.com" in ref_url else "Blog/Write-up"
                    parts.append(f"[{label}: {ref_url}]\n{scraped[:3000]}")
                    logger.info(
                        "GHSA enrichment: scraped %s (%d chars)", ref_url, len(scraped)
                    )
            except Exception as exc:
                logger.warning(
                    "GHSA enrichment: failed to scrape %s: %s", ref_url, exc
                )

        return "\n\n---\n\n".join(parts) if parts else ""

    # ------------------------------------------------------------------
    # RSS collection
    # ------------------------------------------------------------------

    async def _collect_rss(self) -> int:
        """Fetch items from configured RSS feeds.

        Returns:
            Number of newly inserted items.
        """
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
                published = (
                    getattr(entry, "published", None)
                    or datetime.now(timezone.utc).isoformat()
                )

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
    # Twitter/X collection via X API v2
    # ------------------------------------------------------------------

    async def _collect_twitter(self) -> int:
        """Fetch recent tweets from configured handles via X API v2.

        Returns:
            Number of newly inserted items.
        """
        if not self.settings.twitter_bearer_token:
            logger.warning(
                "No X API bearer token configured, skipping Twitter collection"
            )
            return 0

        from_clauses = [f"from:{h}" for h in self.settings.twitter_handles]
        query = f"({' OR '.join(from_clauses)}) -is:retweet"

        users_by_id: dict[str, str] = {}

        count = 0
        next_token: str | None = None
        remaining = self.settings.twitter_max_items

        while remaining > 0:
            params: dict[str, str | int] = {
                "query": query,
                "max_results": max(10, min(remaining, 100)),
                "tweet.fields": "id,text,created_at,author_id",
                "expansions": "author_id",
                "user.fields": "username",
            }
            if next_token:
                params["next_token"] = next_token

            resp = await self._http.get(
                "https://api.x.com/2/tweets/search/recent",
                headers={
                    "Authorization": f"Bearer {self.settings.twitter_bearer_token}",
                },
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
            body = resp.json()

            for user in body.get("includes", {}).get("users", []):
                users_by_id[user["id"]] = user["username"]

            for tweet in body.get("data", []):
                source_id = tweet["id"]
                author_id = tweet.get("author_id", "")
                username = users_by_id.get(author_id, "")
                url = (
                    f"https://x.com/{username}/status/{source_id}"
                    if username
                    else ""
                )
                title = tweet.get("text", "")
                published = tweet.get(
                    "created_at", datetime.now(timezone.utc).isoformat()
                )

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

            next_token = body.get("meta", {}).get("next_token")
            result_count = body.get("meta", {}).get("result_count", 0)
            remaining -= result_count

            if not next_token or result_count == 0:
                break

        return count
