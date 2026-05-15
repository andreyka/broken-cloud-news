"""Control-plane analysis service for claim/retry/persist ownership."""

from __future__ import annotations

import logging
from uuid import UUID

import httpx

from bcn.common.config import Settings
from bcn.contracts.services import AnalystWorkflow
from bcn.persistence.news_items import get_new_items
from bcn.persistence.news_items import release_items_from_analyzing
from bcn.persistence.news_items import update_item_analyzed
from bcn.persistence.runtime import close_pool
from bcn.persistence.runtime import get_pool
from bcn.service_registry import build_analyst_workflow

logger = logging.getLogger(__name__)


def _coerce_uuid(value: object) -> UUID | None:
    """Return a UUID instance when the value is UUID-like."""
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except Exception:
        return None


def _is_rate_limited_error(exc: Exception) -> bool:
    """Return true when analysis failed due to upstream rate limiting."""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        return exc.response.status_code == 429
    return "429 Too Many Requests" in str(exc)


async def execute_analysis(
    settings: Settings,
    *,
    analyst_service: AnalystWorkflow | None = None,
    source: str = "workflow_service",
    manage_pool: bool = True,
) -> str:
    """Claim new items, analyze them, persist results, and release retries."""
    await get_pool(settings)
    active_service = analyst_service or build_analyst_workflow(settings)
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

        analyzed = 0
        failed = 0
        for row in items:
            item = dict(row)
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
                analyzed += 1
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
                                discard_on_exhaustion=not _is_rate_limited_error(exc),
                            )
                        except Exception:
                            logger.exception(
                                "Failed to release ANALYZING item %s after analysis error",
                                item_id,
                            )
                failed += 1

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
