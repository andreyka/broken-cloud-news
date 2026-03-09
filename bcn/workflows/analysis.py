"""Control-plane analysis service for claim/retry/persist ownership."""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from bcn.services.analyst.service import AnalystService
from bcn.common.config import Settings
from bcn.persistence.news_items import get_new_items
from bcn.persistence.news_items import release_items_from_analyzing
from bcn.persistence.news_items import update_item_analyzed
from bcn.persistence.runtime import close_pool
from bcn.persistence.runtime import get_pool

logger = logging.getLogger(__name__)


def _coerce_uuid(value: object) -> UUID | None:
    """Return a UUID instance when the value is UUID-like."""
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except Exception:
        return None


async def _analyze_single_item(
    item: dict,
    active_service: AnalystService,
    settings: Settings,
) -> bool:
    """Analyze a single item, persisting the result or releasing on failure.

    Returns True on success, False on failure.
    """
    try:
        update = await active_service.analyze_item(item)
        await update_item_analyzed(
            item_id=item["id"],
            summary=update.summary,
            relevance_score=update.relevance_score,
            ai_tags=update.ai_tags,
            full_content=update.full_content,
            image_prompt=update.image_prompt,
            canonical_url=update.canonical_url,
        )
        return True
    except Exception as exc:
        logger.exception("Failed to analyze item %s", item.get("id"))
        if str(item.get("status", "")).upper() == "ANALYZING":
            item_id = _coerce_uuid(item.get("id"))
            if item_id is not None:
                try:
                    await release_items_from_analyzing(
                        [item_id],
                        error=f"{type(exc).__name__}: {exc}",
                        max_retries=settings.analysis_retry_max_attempts,
                        base_delay_seconds=(
                            settings.analysis_retry_base_delay_seconds
                        ),
                        max_delay_seconds=(
                            settings.analysis_retry_max_delay_seconds
                        ),
                    )
                except Exception:
                    logger.exception(
                        "Failed to release ANALYZING item %s after analysis error",
                        item_id,
                    )
        return False


async def execute_analysis(
    settings: Settings,
    *,
    analyst_service: AnalystService | None = None,
    source: str = "workflow_service",
    manage_pool: bool = True,
) -> str:
    """Claim new items, analyze them concurrently, persist results, and release retries."""
    await get_pool(settings)
    active_service = analyst_service or AnalystService(settings)
    owns_service = analyst_service is None

    try:
        items = await get_new_items(
            stale_analyzing_minutes=settings.analysis_retry_stale_analyzing_minutes,
            max_analysis_retries=settings.analysis_retry_max_attempts,
        )
        if not items:
            message = "No new items to analyze"
            logger.info("%s [source=%s]", message, source)
            return message

        semaphore = asyncio.Semaphore(settings.analysis_concurrency)

        async def _bounded(item: dict) -> bool:
            async with semaphore:
                return await _analyze_single_item(item, active_service, settings)

        results = await asyncio.gather(
            *[_bounded(dict(row)) for row in items],
            return_exceptions=False,
        )

        analyzed = sum(1 for r in results if r)
        failed = sum(1 for r in results if not r)

        message = f"Analyzed {analyzed}/{len(items)} items"
        if failed:
            message += f" ({failed} failed)"
        logger.info("%s [source=%s]", message, source)
        return message
    finally:
        if owns_service:
            await active_service.close()
        if manage_pool:
            await close_pool()
