"""Reddit collection adapter for the collector service."""

from __future__ import annotations

import logging
from typing import Any

import feedparser

from bcn.common.models import CollectedNewsItem
from bcn.services.collector.common import build_reddit_full_content
from bcn.services.collector.common import clean_summary
from bcn.services.collector.common import extract_feed_published_at
from bcn.services.collector.common import extract_reddit_post_id
from bcn.services.collector.common import extract_reddit_reference_urls
from bcn.services.collector.common import is_cloud_security_relevant
from bcn.services.collector.common import normalize_reddit_permalink
from bcn.services.collector.common import validate_source_timestamp

logger = logging.getLogger(__name__)


async def collect_reddit_items(service: Any) -> list[CollectedNewsItem]:
    """Fetch recent posts from configured subreddits via RSS + Reddit JSON."""
    items: list[CollectedNewsItem] = []
    for subreddit in service.settings.reddit_subreddits:
        feed_url = f"https://www.reddit.com/r/{subreddit}/.rss"
        engagement_map = await fetch_reddit_engagement(service, subreddit)
        try:
            feed_text = await service.scraper.fetch_text_or_raise(
                feed_url,
                headers={
                    "User-Agent": "BrokenCloudNews/1.0 (cloud-security digest bot)"
                },
                timeout_ms=30000,
            )
            feed = feedparser.parse(feed_text)
        except Exception as exc:
            logger.warning("Failed to fetch Reddit feed %s: %s", feed_url, exc)
            continue

        for entry in feed.entries:
            source_id = getattr(entry, "id", None) or getattr(entry, "link", "")
            permalink = str(getattr(entry, "link", "") or "").strip()
            title = getattr(entry, "title", "")
            summary = clean_summary(getattr(entry, "summary", ""))
            published_at, published_raw, published_field = extract_feed_published_at(entry)
            published_at = validate_source_timestamp(
                published_at,
                source_type="reddit",
                source_id=str(source_id or ""),
                title=str(title or ""),
                url=str(permalink or ""),
                field=published_field,
            )
            if published_at is None:
                continue
            published = published_at.isoformat()

            text_for_filter = f"{title} {summary} r/{subreddit}"
            if not is_cloud_security_relevant(service.settings, text_for_filter):
                continue

            post_id = extract_reddit_post_id(source_id, permalink)
            engagement = engagement_map.get(post_id, {})
            references = extract_reddit_reference_urls(permalink, engagement)
            full_content = build_reddit_full_content(title, summary, references)
            items.append(
                CollectedNewsItem(
                    source_type="reddit",
                    source_id=source_id,
                    url=permalink,
                    title=title,
                    published_at=published_at,
                    raw_data={
                        "subreddit": subreddit,
                        "feed_url": feed_url,
                        "title": title,
                        "link": permalink,
                        "permalink": permalink,
                        "published": published,
                        "published_raw": published_raw,
                        "published_field": published_field,
                        "summary": summary,
                        "engagement": engagement,
                        "references": [{"url": ref} for ref in references],
                    },
                    full_content=full_content,
                )
            )

    return items


async def fetch_reddit_engagement(
    service: Any,
    subreddit: str,
) -> dict[str, dict[str, Any]]:
    """Fetch engagement and outbound URL metadata via the Reddit JSON API."""
    url = f"https://www.reddit.com/r/{subreddit}/new.json?limit=100"
    try:
        payload = await service.scraper.fetch_json(
            url,
            headers={"User-Agent": "BrokenCloudNews/1.0 (cloud-security digest bot)"},
            timeout_ms=20000,
        )
    except Exception as exc:
        logger.warning("Failed to fetch Reddit metrics %s: %s", url, exc)
        return {}

    out: dict[str, dict[str, Any]] = {}
    children = payload.get("data", {}).get("children", [])
    for child in children:
        data = child.get("data", {})
        post_id = str(data.get("id") or "").strip()
        if not post_id:
            continue
        out[post_id] = {
            "upvotes": float(data.get("ups") or data.get("score") or 0),
            "comments": float(data.get("num_comments") or 0),
            "upvote_ratio": float(data.get("upvote_ratio") or 0),
            "url": str(data.get("url") or "").strip(),
            "url_overridden_by_dest": str(data.get("url_overridden_by_dest") or "").strip(),
            "permalink": normalize_reddit_permalink(
                str(data.get("permalink") or "").strip()
            ),
        }
    return out


__all__ = [
    "collect_reddit_items",
    "fetch_reddit_engagement",
]
