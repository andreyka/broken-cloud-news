"""RSS collection adapter for the collector service."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import logging
from typing import Any

import feedparser

from bcn.common.models import CollectedNewsItem
from bcn.services.collector.common import clean_summary
from bcn.services.collector.common import extract_feed_published_at
from bcn.services.collector.common import is_cloud_security_relevant
from bcn.services.collector.common import validate_source_timestamp

logger = logging.getLogger(__name__)


async def collect_rss_items(service: Any) -> list[CollectedNewsItem]:
    """Fetch items from configured RSS feeds."""
    items: list[CollectedNewsItem] = []
    max_entries = max(1, int(service.settings.collector_rss_max_entries_per_feed))
    max_age_days = max(0, int(service.settings.collector_rss_max_item_age_days))
    max_age = timedelta(days=max_age_days) if max_age_days > 0 else None
    scrape_budget = max(
        0, int(service.settings.collector_rss_full_content_limit_per_feed)
    )
    scrape_timeout_ms = max(1000, int(service.settings.collector_rss_scrape_timeout_ms))
    now = datetime.now(timezone.utc)
    for feed_url in service.settings.rss_feeds:
        feed_total_entries = 0
        considered_entries = 0
        relevant_entries = 0
        kept_entries = 0
        old_entries = 0
        scrape_count = 0
        skipped_scrapes = 0
        try:
            feed_text = await service.scraper.fetch_text_or_raise(
                feed_url,
                timeout_ms=30000,
            )
            feed = feedparser.parse(feed_text)
        except Exception as exc:
            logger.warning("Failed to fetch RSS %s: %s", feed_url, exc)
            continue

        feed_total_entries = len(feed.entries)
        for entry in list(feed.entries)[:max_entries]:
            considered_entries += 1
            source_id = getattr(entry, "id", None) or getattr(entry, "link", "")
            url = getattr(entry, "link", "")
            title = getattr(entry, "title", "")
            summary = clean_summary(getattr(entry, "summary", ""))
            published_at, published_raw, published_field = extract_feed_published_at(entry)
            published_at = validate_source_timestamp(
                published_at,
                source_type="rss",
                source_id=str(source_id or ""),
                title=str(title or ""),
                url=str(url or ""),
                field=published_field,
            )
            if published_at is None:
                continue
            if max_age is not None and published_at < now - max_age:
                old_entries += 1
                continue
            published = published_at.isoformat()

            if not is_cloud_security_relevant(service.settings, f"{title} {summary}"):
                continue
            relevant_entries += 1

            full_content = ""
            if url and scrape_count < scrape_budget:
                full_content = await service.scraper.scrape(
                    url,
                    timeout_ms=scrape_timeout_ms,
                    settle_ms=1000,
                )
                scrape_count += 1
            elif url:
                skipped_scrapes += 1

            items.append(
                CollectedNewsItem(
                    source_type="rss",
                    source_id=source_id,
                    url=url,
                    title=title,
                    published_at=published_at,
                    raw_data={
                        "feed_url": feed_url,
                        "title": title,
                        "link": url,
                        "published": published,
                        "published_raw": published_raw,
                        "published_field": published_field,
                        "summary": summary,
                    },
                    full_content=full_content or None,
                )
            )
            kept_entries += 1

        logger.info(
            "RSS feed processed %s: total=%d considered=%d kept=%d relevant=%d "
            "old_skipped=%d scraped=%d scrape_budget_skipped=%d",
            feed_url,
            feed_total_entries,
            considered_entries,
            kept_entries,
            relevant_entries,
            old_entries,
            scrape_count,
            skipped_scrapes,
        )

    return items


__all__ = ["collect_rss_items"]
