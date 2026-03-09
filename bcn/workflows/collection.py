"""Control-plane collection service for source fan-out and persistence."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Any
from typing import Literal

from bcn.services.collector.review import SourceReviewLLM
from bcn.common.config import Settings
from bcn.common.llm import LLMClient
from bcn.common.models import CollectedNewsItem
from bcn.contracts.services import CollectorWorkflow
from bcn.persistence.collection_sources import collection_source_has_historical_items
from bcn.persistence.collection_sources import get_collection_source
from bcn.persistence.collection_sources import record_collection_source_review
from bcn.persistence.collection_sources import upsert_collection_source
from bcn.persistence.news_items import insert_news_item
from bcn.persistence.runtime import close_pool
from bcn.persistence.runtime import get_pool
from bcn.service_registry import build_collector_workflow

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


@dataclass(frozen=True)
class _CollectionSourceDescriptor:
    source_key: str
    source_type: str
    display_name: str
    raw_config: dict[str, Any]


def _describe_source(item: CollectedNewsItem) -> _CollectionSourceDescriptor:
    """Resolve a stable registry identity for one collected item."""
    raw = item.raw_data if isinstance(item.raw_data, dict) else {}
    source_type = str(item.source_type or "").strip().lower()

    if source_type == "rss":
        feed_url = str(raw.get("feed_url") or "").strip()
        key = f"rss:{feed_url.lower()}" if feed_url else "rss:unknown"
        return _CollectionSourceDescriptor(
            source_key=key,
            source_type="rss",
            display_name=feed_url or "unknown rss feed",
            raw_config={"feed_url": feed_url},
        )

    if source_type == "reddit":
        subreddit = str(raw.get("subreddit") or "").strip().lower()
        label = f"r/{subreddit}" if subreddit else "unknown subreddit"
        key = f"reddit:{label}" if subreddit else "reddit:unknown"
        return _CollectionSourceDescriptor(
            source_key=key,
            source_type="reddit",
            display_name=label,
            raw_config={"subreddit": subreddit},
        )

    if source_type == "twitter":
        username = str(raw.get("username") or "").strip().lower()
        label = f"@{username}" if username else "unknown account"
        key = f"twitter:{label}" if username else "twitter:unknown"
        return _CollectionSourceDescriptor(
            source_key=key,
            source_type="twitter",
            display_name=label,
            raw_config={"username": username},
        )

    return _CollectionSourceDescriptor(
        source_key="ghsa:github_security_advisories",
        source_type="ghsa",
        display_name="GitHub Security Advisories",
        raw_config={"source_name": "github_security_advisories"},
    )


def _group_items_by_source(
    items: list[CollectedNewsItem],
) -> list[tuple[_CollectionSourceDescriptor, list[CollectedNewsItem]]]:
    """Group collected items by their source registry identity."""
    groups: dict[str, tuple[_CollectionSourceDescriptor, list[CollectedNewsItem]]] = {}
    for item in items:
        descriptor = _describe_source(item)
        if descriptor.source_key not in groups:
            groups[descriptor.source_key] = (descriptor, [])
        groups[descriptor.source_key][1].append(item)
    return list(groups.values())


async def _source_is_active(
    descriptor: _CollectionSourceDescriptor,
    items: list[CollectedNewsItem],
    *,
    settings: Settings,
    reviewer: SourceReviewLLM | None,
) -> bool:
    """Return whether a source is allowed to write into production news_items."""
    row = await get_collection_source(descriptor.source_key)
    if row is not None:
        state = str(row["state"] or "").upper()
        if state == "ACTIVE":
            await upsert_collection_source(
                source_key=descriptor.source_key,
                source_type=descriptor.source_type,
                display_name=descriptor.display_name,
                state="ACTIVE",
                raw_config=descriptor.raw_config,
            )
            return True
        if state == "DISABLED":
            logger.warning("Skipping disabled source %s", descriptor.source_key)
            return False
        if state == "QUARANTINED":
            logger.warning("Skipping quarantined source %s", descriptor.source_key)
            return False
        if state == "PENDING_REVIEW" and not settings.source_review_enabled:
            logger.warning(
                "Skipping pending-review source %s while source review is disabled",
                descriptor.source_key,
            )
            return False

    if not settings.source_review_enabled:
        return True

    if await collection_source_has_historical_items(
        source_type=descriptor.source_type,
        raw_config=descriptor.raw_config,
    ):
        await upsert_collection_source(
            source_key=descriptor.source_key,
            source_type=descriptor.source_type,
            display_name=descriptor.display_name,
            state="ACTIVE",
            raw_config=descriptor.raw_config,
            review_reason="preexisting_source",
            review_payload={"decision": "promote", "origin": "historical_items"},
        )
        return True

    await upsert_collection_source(
        source_key=descriptor.source_key,
        source_type=descriptor.source_type,
        display_name=descriptor.display_name,
        state="PENDING_REVIEW",
        raw_config=descriptor.raw_config,
        review_reason="awaiting_source_review",
    )
    if reviewer is None:
        await upsert_collection_source(
            source_key=descriptor.source_key,
            source_type=descriptor.source_type,
            display_name=descriptor.display_name,
            state="ACTIVE",
            raw_config=descriptor.raw_config,
            review_reason="source_review_disabled",
            review_payload={"decision": "promote", "origin": "review_disabled"},
        )
        return True

    sample_size = max(1, int(settings.source_review_sample_size))
    try:
        review = await reviewer.review_source(
            source_type=descriptor.source_type,
            display_name=descriptor.display_name,
            raw_config=descriptor.raw_config,
            sample_items=items[:sample_size],
        )
    except Exception as exc:
        await upsert_collection_source(
            source_key=descriptor.source_key,
            source_type=descriptor.source_type,
            display_name=descriptor.display_name,
            state="PENDING_REVIEW",
            raw_config=descriptor.raw_config,
            review_reason=f"source_review_error: {type(exc).__name__}",
            review_payload={"error": str(exc), "origin": "source_review_error"},
        )
        logger.warning(
            "Left new source %s pending review after source review error: %s",
            descriptor.source_key,
            exc,
        )
        return False
    review_payload = review.model_dump()
    await record_collection_source_review(
        source_key=descriptor.source_key,
        decision=review.decision,
        confidence=review.confidence,
        rationale=review.rationale,
        review_payload=review_payload,
    )
    await upsert_collection_source(
        source_key=descriptor.source_key,
        source_type=descriptor.source_type,
        display_name=descriptor.display_name,
        state="ACTIVE" if review.decision == "promote" else "QUARANTINED",
        raw_config=descriptor.raw_config,
        review_reason=review.rationale or review.decision,
        review_payload=review_payload,
    )
    if review.decision != "promote":
        logger.warning(
            "Quarantined new source %s after LLM review: %s",
            descriptor.source_key,
            review.rationale or review.decision,
        )
        return False
    return True


async def _persist_collected_items(
    items: list[CollectedNewsItem],
    *,
    max_reddit_items_per_subreddit: int | None = None,
    settings: Settings,
    reviewer: SourceReviewLLM | None,
) -> int:
    """Insert collected items and return how many were newly created."""
    inserted_count = 0
    inserted_reddit_counts: dict[str, int] = {}
    allowed_items: list[CollectedNewsItem] = []
    for descriptor, group in _group_items_by_source(items):
        if await _source_is_active(
            descriptor,
            group,
            settings=settings,
            reviewer=reviewer,
        ):
            allowed_items.extend(group)

    for item in allowed_items:
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
    collector_service: CollectorWorkflow,
    source: Literal["ghsa", "rss", "twitter", "reddit"],
    reviewer: SourceReviewLLM | None,
) -> CollectionRunResult:
    """Run one collector source and persist its items."""
    items = await _collect_from_service(collector_service, source)
    inserted_count = await _persist_collected_items(
        items,
        settings=settings,
        reviewer=reviewer,
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
    collector_service: CollectorWorkflow,
    reviewer: SourceReviewLLM | None,
) -> CollectionRunResult:
    """Run all collector sources concurrently and persist successes."""
    coroutines = [
        _collect_from_service(collector_service, source) for source in _SOURCE_ORDER
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
                settings=settings,
                reviewer=reviewer,
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


async def _collect_from_service(
    collector_service: CollectorWorkflow,
    source: Literal["ghsa", "rss", "twitter", "reddit"],
) -> list[CollectedNewsItem]:
    """Collect from one source through the typed collector workflow contract."""
    return await collector_service.collect(source)


async def execute_collection(
    settings: Settings,
    *,
    source: CollectionSource = "all",
    collector_service: CollectorWorkflow | None = None,
    llm_client: LLMClient | None = None,
    origin: str = "workflow_service",
    manage_pool: bool = True,
) -> str:
    """Collect from one or all sources, persist results, and return a summary."""
    await get_pool(settings)
    active_service = collector_service or build_collector_workflow(settings)
    owns_service = collector_service is None
    active_llm_client: LLMClient | None = None
    owns_llm_client = False
    reviewer: SourceReviewLLM | None = None
    if settings.source_review_enabled:
        active_llm_client = llm_client or LLMClient.from_settings(settings)
        owns_llm_client = llm_client is None
        reviewer = SourceReviewLLM(active_llm_client)

    try:
        if source == "all":
            result = await _run_all_sources(
                settings,
                collector_service=active_service,
                reviewer=reviewer,
            )
        else:
            result = await _run_single_source(
                settings,
                collector_service=active_service,
                source=source,
                reviewer=reviewer,
            )

        message = result.render_message()
        logger.info("%s [origin=%s]", message, origin)
        return message
    finally:
        if owns_service:
            await active_service.close()
        if owns_llm_client and active_llm_client is not None:
            await active_llm_client.close()
        if manage_pool:
            await close_pool()


__all__ = [
    "CollectionRunResult",
    "CollectionSource",
    "execute_collection",
]
