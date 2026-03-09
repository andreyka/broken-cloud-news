"""Twitter/X collection adapter for the collector service."""

from __future__ import annotations

import logging
from typing import Any

from bcn.common.models import CollectedNewsItem
from bcn.services.collector.common import build_tweet_full_content
from bcn.services.collector.common import coerce_feed_datetime
from bcn.services.collector.common import extract_tweet_reference_urls
from bcn.services.collector.common import is_cloud_security_relevant
from bcn.services.collector.common import validate_source_timestamp

logger = logging.getLogger(__name__)


async def collect_twitter_items(service: Any) -> list[CollectedNewsItem]:
    """Fetch recent tweets from configured handles via X API v2."""
    if not service.settings.twitter_bearer_token:
        logger.warning(
            "No X API bearer token configured, skipping Twitter collection"
        )
        return []

    from_clauses = [f"from:{handle}" for handle in service.settings.twitter_handles]
    query = f"({' OR '.join(from_clauses)}) -is:retweet"
    users_by_id: dict[str, str] = {}
    items: list[CollectedNewsItem] = []

    next_token: str | None = None
    remaining = service.settings.twitter_max_items
    while remaining > 0:
        params: dict[str, str | int] = {
            "query": query,
            "max_results": max(10, min(remaining, 100)),
            "tweet.fields": "id,text,created_at,author_id,public_metrics,entities",
            "expansions": "author_id",
            "user.fields": "username",
        }
        if next_token:
            params["next_token"] = next_token

        response = await service._http.get(
            "https://api.x.com/2/tweets/search/recent",
            headers={
                "Authorization": f"Bearer {service.settings.twitter_bearer_token}",
            },
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        body = response.json()

        for user in body.get("includes", {}).get("users", []):
            users_by_id[user["id"]] = user["username"]

        for tweet in body.get("data", []):
            source_id = tweet["id"]
            author_id = tweet.get("author_id", "")
            username = users_by_id.get(author_id, "")
            url = f"https://x.com/{username}/status/{source_id}" if username else ""
            title = tweet.get("text", "")
            if not is_cloud_security_relevant(service.settings, title):
                continue
            published_at = validate_source_timestamp(
                coerce_feed_datetime(tweet.get("created_at")),
                source_type="twitter",
                source_id=str(source_id or ""),
                title=str(title or ""),
                url=str(url or ""),
                field="created_at",
            )
            if published_at is None:
                continue
            references = extract_tweet_reference_urls(tweet)
            full_content = build_tweet_full_content(title, references)
            items.append(
                CollectedNewsItem(
                    source_type="twitter",
                    source_id=source_id,
                    url=url,
                    title=title,
                    published_at=published_at,
                    raw_data={
                        **tweet,
                        "username": username,
                        "references": [{"url": ref} for ref in references],
                    },
                    full_content=full_content,
                )
            )

        next_token = body.get("meta", {}).get("next_token")
        result_count = body.get("meta", {}).get("result_count", 0)
        remaining -= result_count
        if not next_token or result_count == 0:
            break

    return items


__all__ = ["collect_twitter_items"]
