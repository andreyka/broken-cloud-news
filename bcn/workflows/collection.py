"""Control-plane collection service for source fan-out and persistence."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Awaitable
from typing import Callable
from typing import Literal

from bcn.agents.collector.service import CollectorService
from bcn.common.config import Settings
from bcn.common.db import close_pool
from bcn.common.db import get_pool
from bcn.common.db import insert_news_item
from bcn.common.models import CollectedNewsItem

logger = logging.getLogger(__name__)

CollectionSource = Literal["all", "ghsa", "rss", "twitter", "reddit"]
_SOURCE_ORDER = ("ghsa", "rss", "twitter", "reddit")
_SOURCE_LABELS = {
    "ghsa": "GHSA",
    "rss": "RSS",
    "twitter": "Twitter",
    "reddit": "Reddit",
}


@dataclass(frozen=True)
class CollectionRunResult:
    """Structured collection outcome returned by the control plane."""

    source: CollectionSource
    counts: dict[str, int]
    failures: dict[str, str]

    def render_message(self) -> str:
        """Render the user-facing collection summary."""
        if self.source != "all":
            label = _SOURCE_LABELS[self.source]
            return f"{label}: collected {self.counts.get(self.source, 0)} items"

        message = (
            "All: "
            f"GHSA={self.counts.get('ghsa', 0)}, "
            f"RSS={self.counts.get('rss', 0)}, "
            f"Twitter={self.counts.get('twitter', 0)}, "
            f"Reddit={self.counts.get('reddit', 0)}"
        )
        if self.failures:
            failed = ", ".join(sorted(self.failures))
            message += f" (failures: {failed})"
        return message


async def _persist_collected_items(
    items: list[CollectedNewsItem],
    *,
    max_reddit_items_per_subreddit: int | None = None,
) -> int:
    """Insert collected items and return how many were newly created."""
    inserted_count = 0
    inserted_reddit_counts: dict[str, int] = {}
    for item in items:
        subreddit = ""
        if (
            max_reddit_items_per_subreddit is not None
            and item.source_type == "reddit"
            and isinstance(item.raw_data, dict)
        ):
            subreddit = str(item.raw_data.get("subreddit") or "").strip().lower()
            if (
                subreddit
                and inserted_reddit_counts.get(subreddit, 0)
                >= max_reddit_items_per_subreddit
            ):
                continue

        inserted = await insert_news_item(
            source_type=item.source_type,
            source_id=item.source_id,
            url=item.url,
            title=item.title,
            published_at=item.published_at,
            raw_data=item.raw_data,
            full_content=item.full_content,
        )
        if inserted:
            inserted_count += 1
            if subreddit:
                inserted_reddit_counts[subreddit] = (
                    inserted_reddit_counts.get(subreddit, 0) + 1
                )
    return inserted_count


def _collect_method(
    collector_service: CollectorService,
    source: Literal["ghsa", "rss", "twitter", "reddit"],
) -> Callable[[], Awaitable[list[CollectedNewsItem]]]:
    """Return the matching collector service method for one source."""
    if source == "ghsa":
        return collector_service.collect_ghsa_items
    if source == "rss":
        return collector_service.collect_rss_items
    if source == "twitter":
        return collector_service.collect_twitter_items
    return collector_service.collect_reddit_items


def _record_failure(
    *,
    source: str,
    error: BaseException,
    failures: dict[str, str],
) -> None:
    """Store and log one per-source collection failure."""
    failures[source] = f"{error.__class__.__name__}: {error}"
    logger.error(
        "Collector source %s failed",
        source,
        exc_info=(error.__class__, error, error.__traceback__),
    )


async def _run_single_source(
    settings: Settings,
    *,
    collector_service: CollectorService,
    source: Literal["ghsa", "rss", "twitter", "reddit"],
) -> CollectionRunResult:
    """Run one collector source and persist its items."""
    items = await _collect_method(collector_service, source)()
    inserted_count = await _persist_collected_items(
        items,
        max_reddit_items_per_subreddit=(
            settings.reddit_max_items_per_subreddit if source == "reddit" else None
        ),
    )
    return CollectionRunResult(
        source=source,
        counts={source: inserted_count},
        failures={},
    )


async def _run_all_sources(
    settings: Settings,
    *,
    collector_service: CollectorService,
) -> CollectionRunResult:
    """Run all collector sources concurrently and persist successes."""
    coroutines = [
        _collect_method(collector_service, source)()
        for source in _SOURCE_ORDER
    ]
    raw_results = await asyncio.gather(*coroutines, return_exceptions=True)

    counts: dict[str, int] = {}
    failures: dict[str, str] = {}
    for source, result in zip(_SOURCE_ORDER, raw_results):
        if isinstance(result, BaseException):
            counts[source] = 0
            _record_failure(source=source, error=result, failures=failures)
            continue

        try:
            counts[source] = await _persist_collected_items(
                result,
                max_reddit_items_per_subreddit=(
                    settings.reddit_max_items_per_subreddit
                    if source == "reddit"
                    else None
                ),
            )
        except Exception as exc:
            counts[source] = 0
            _record_failure(source=source, error=exc, failures=failures)

    return CollectionRunResult(source="all", counts=counts, failures=failures)


async def execute_collection(
    settings: Settings,
    *,
    source: CollectionSource = "all",
    collector_service: CollectorService | None = None,
    origin: str = "workflow_service",
    manage_pool: bool = True,
) -> str:
    """Collect from one or all sources, persist results, and return a summary."""
    await get_pool(settings)
    active_service = collector_service or CollectorService(settings)
    owns_service = collector_service is None

    try:
        if source == "all":
            result = await _run_all_sources(
                settings,
                collector_service=active_service,
            )
        else:
            result = await _run_single_source(
                settings,
                collector_service=active_service,
                source=source,
            )

        message = result.render_message()
        logger.info("%s [origin=%s]", message, origin)
        return message
    finally:
        if owns_service:
            await active_service.close()
        if manage_pool:
            await close_pool()


__all__ = [
    "CollectionRunResult",
    "CollectionSource",
    "execute_collection",
]
