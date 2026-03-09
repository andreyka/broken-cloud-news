"""Collector domain service shared by the control plane and service workers."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from bcn.common.config import Settings
from bcn.common.models import CollectedNewsItem
from bcn.common.scraper import Scraper
from bcn.services.collector.common import build_reddit_full_content
from bcn.services.collector.common import build_tweet_full_content
from bcn.services.collector.common import clean_summary
from bcn.services.collector.common import coerce_feed_datetime
from bcn.services.collector.common import extract_feed_published_at
from bcn.services.collector.common import extract_reddit_post_id
from bcn.services.collector.common import extract_reddit_reference_urls
from bcn.services.collector.common import extract_tweet_reference_urls
from bcn.services.collector.common import feed_entry_value
from bcn.services.collector.common import is_cloud_security_relevant
from bcn.services.collector.common import is_internal_reddit_url
from bcn.services.collector.common import is_internal_twitter_url
from bcn.services.collector.common import is_useful_reddit_reference
from bcn.services.collector.common import normalize_reddit_permalink
from bcn.services.collector.common import select_reddit_primary_url
from bcn.services.collector.common import validate_source_timestamp
from bcn.services.collector.ghsa import GHSA_QUERY
from bcn.services.collector.ghsa import collect_ghsa_items as run_collect_ghsa_items
from bcn.services.collector.ghsa import enrich_ghsa_content as run_enrich_ghsa_content
from bcn.services.collector.reddit import (
    collect_reddit_items as run_collect_reddit_items,
)
from bcn.services.collector.reddit import (
    fetch_reddit_engagement as run_fetch_reddit_engagement,
)
from bcn.services.collector.rss import collect_rss_items as run_collect_rss_items
from bcn.services.collector.twitter import (
    collect_twitter_items as run_collect_twitter_items,
)

logger = logging.getLogger(__name__)


class CollectorService:
    """Thin facade over source-specific collector adapters."""

    def __init__(
        self,
        settings: Settings,
        *,
        scraper: Scraper | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._owns_scraper = scraper is None
        self._owns_http_client = http_client is None
        self.scraper = scraper if scraper is not None else Scraper(
            content_limit=settings.scrape_content_limit,
            min_content_length=settings.scrape_min_content_length,
        )
        self._http = (
            http_client if http_client is not None else httpx.AsyncClient(timeout=60)
        )

    async def close(self) -> None:
        """Release resources owned by this collector service."""
        if self._owns_scraper:
            await self.scraper.close()
        if self._owns_http_client:
            await self._http.aclose()

    async def collect(self, source: str) -> list[CollectedNewsItem]:
        """Collect items for one normalized source label."""
        normalized = str(source or "").strip().lower()
        if normalized == "ghsa":
            return await self.collect_ghsa_items()
        if normalized == "rss":
            return await self.collect_rss_items()
        if normalized == "twitter":
            return await self.collect_twitter_items()
        if normalized == "reddit":
            return await self.collect_reddit_items()
        raise ValueError(f"Unsupported collector source: {source}")

    def _is_cloud_security_relevant(self, text: str) -> bool:
        """Return whether the text looks relevant to cloud security."""
        return is_cloud_security_relevant(self.settings, text)

    @staticmethod
    def _clean_summary(value: str) -> str:
        """Strip basic HTML tags/entities from feed summaries."""
        return clean_summary(value)

    @staticmethod
    def _feed_entry_value(entry: Any, field: str) -> Any:
        """Read one feed field without feedparser's attribute alias fallback."""
        return feed_entry_value(entry, field)

    @staticmethod
    def _coerce_feed_datetime(value: Any):
        """Normalize feed date values into aware UTC datetimes."""
        return coerce_feed_datetime(value)

    @staticmethod
    def _validate_source_timestamp(
        published_at: Any,
        *,
        source_type: str,
        source_id: str,
        title: str,
        url: str,
        field: str,
    ):
        """Drop items with missing or implausible source timestamps."""
        return validate_source_timestamp(
            published_at,
            source_type=source_type,
            source_id=source_id,
            title=title,
            url=url,
            field=field,
        )

    def _extract_feed_published_at(self, entry: Any):
        """Return the best available feed timestamp for one entry."""
        return extract_feed_published_at(entry)

    async def collect_ghsa_items(self) -> list[CollectedNewsItem]:
        """Fetch GitHub Security Advisories matching cloud keywords."""
        return await run_collect_ghsa_items(self)

    async def _enrich_ghsa_content(
        self,
        item: dict[str, Any],
        references: list[str],
    ) -> str:
        """Build enriched content for a GHSA item by scraping reference links."""
        return await run_enrich_ghsa_content(self, item, references)

    async def collect_rss_items(self) -> list[CollectedNewsItem]:
        """Fetch items from configured RSS feeds."""
        return await run_collect_rss_items(self)

    async def collect_twitter_items(self) -> list[CollectedNewsItem]:
        """Fetch recent tweets from configured handles via X API v2."""
        return await run_collect_twitter_items(self)

    async def collect_reddit_items(self) -> list[CollectedNewsItem]:
        """Fetch recent posts from configured subreddits via RSS + Reddit JSON."""
        return await run_collect_reddit_items(self)

    async def _fetch_reddit_engagement(
        self,
        subreddit: str,
    ) -> dict[str, dict[str, Any]]:
        """Fetch engagement and outbound URL metadata via the Reddit JSON API."""
        return await run_fetch_reddit_engagement(self, subreddit)

    @staticmethod
    def _extract_reddit_post_id(source_id: str, url: str) -> str:
        """Extract the Reddit post id from a feed source id or permalink."""
        return extract_reddit_post_id(source_id, url)

    @staticmethod
    def _extract_tweet_reference_urls(tweet: dict[str, Any]) -> list[str]:
        """Extract external reference URLs from tweet entities."""
        return extract_tweet_reference_urls(tweet)

    @staticmethod
    def _is_internal_twitter_url(url: str) -> bool:
        """Return whether a URL points back to X/Twitter itself."""
        return is_internal_twitter_url(url)

    @staticmethod
    def _extract_reddit_reference_urls(
        permalink: str,
        metadata: dict[str, Any],
    ) -> list[str]:
        """Extract non-Reddit outbound URLs from Reddit post metadata."""
        return extract_reddit_reference_urls(permalink, metadata)

    @staticmethod
    def _select_reddit_primary_url(
        permalink: str,
        references: list[str],
        *,
        title: str = "",
        summary: str = "",
    ) -> str:
        """Prefer outbound source only when it looks technically useful."""
        return select_reddit_primary_url(
            permalink,
            references,
            title=title,
            summary=summary,
        )

    @staticmethod
    def _is_internal_reddit_url(url: str) -> bool:
        """Return whether a URL points to Reddit-owned domains."""
        return is_internal_reddit_url(url)

    @staticmethod
    def _is_useful_reddit_reference(url: str, title: str, summary: str) -> bool:
        """Return whether an outbound Reddit link looks technically useful."""
        return is_useful_reddit_reference(url, title, summary)

    @staticmethod
    def _normalize_reddit_permalink(permalink: str) -> str:
        """Normalize Reddit relative permalinks to absolute URLs."""
        return normalize_reddit_permalink(permalink)

    @staticmethod
    def _build_reddit_full_content(
        title: str,
        summary: str,
        references: list[str],
    ) -> str | None:
        """Compose analysis-friendly Reddit content with outbound references."""
        return build_reddit_full_content(title, summary, references)

    @staticmethod
    def _build_tweet_full_content(
        tweet_text: str,
        references: list[str],
    ) -> str | None:
        """Compose analysis-friendly tweet content with extracted references."""
        return build_tweet_full_content(tweet_text, references)


__all__ = [
    "CollectorService",
    "GHSA_QUERY",
]
