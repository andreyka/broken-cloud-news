"""Async PostgreSQL database layer using asyncpg."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import asyncpg

from bcn.config import Settings

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None


async def get_pool(settings: Optional[Settings] = None) -> asyncpg.Pool:
    """Return the shared connection pool, creating it on first call.

    Args:
        settings: Optional settings override. Uses defaults when ``None``.

    Returns:
        The asyncpg connection pool.
    """
    global _pool
    if _pool is None:
        s = settings or Settings()
        _pool = await asyncpg.create_pool(s.database_url, min_size=2, max_size=10)
    return _pool


async def close_pool() -> None:
    """Close the shared connection pool if it is open."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


# ---------------------------------------------------------------------------
# News Items
# ---------------------------------------------------------------------------


async def insert_news_item(
    source_type: str,
    source_id: str,
    url: str,
    title: Optional[str],
    published_at: str | datetime,
    raw_data: dict,
    full_content: Optional[str] = None,
) -> Optional[UUID]:
    """Insert a news item, skipping duplicates via ``ON CONFLICT``.

    Args:
        source_type: Origin of the item (``ghsa``, ``rss``, ``twitter``).
        source_id: Unique identifier within the source.
        url: Canonical URL for the item.
        title: Human-readable title (may be ``None``).
        published_at: ISO-8601 timestamp or ``datetime`` object.
        raw_data: Original payload stored as JSONB.
        full_content: Scraped or enriched body text.

    Returns:
        The UUID of the newly inserted row, or ``None`` if it already existed.
    """
    if isinstance(published_at, str):
        try:
            pub_dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pub_dt = datetime.now(timezone.utc)
    else:
        pub_dt = published_at

    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO news_items (source_type, source_id, url, title, published_at, raw_data, full_content, status)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, 'NEW')
        ON CONFLICT (source_type, source_id) DO NOTHING
        RETURNING id
        """,
        source_type,
        source_id,
        url,
        title,
        pub_dt,
        json.dumps(raw_data),
        full_content,
    )
    return row["id"] if row else None


async def get_new_items() -> list[asyncpg.Record]:
    """Fetch all news items with status ``NEW``."""
    pool = await get_pool()
    return await pool.fetch("SELECT * FROM news_items WHERE status = 'NEW'")


async def update_item_scraped(item_id: UUID, full_content: str) -> None:
    """Mark an item as ``SCRAPED`` and store the scraped body text.

    Args:
        item_id: Primary key of the item.
        full_content: The scraped body text.
    """
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE news_items
        SET full_content = $1, status = 'SCRAPED', updated_at = NOW()
        WHERE id = $2
        """,
        full_content,
        item_id,
    )


async def update_item_analyzed(
    item_id: UUID,
    summary: str,
    relevance_score: int,
    ai_tags: list[str],
    full_content: Optional[str],
    image_prompt: Optional[str],
) -> None:
    """Mark an item as ``ANALYZED`` and store LLM analysis results.

    Args:
        item_id: Primary key of the item.
        summary: LLM-generated summary.
        relevance_score: Relevance score (1-10).
        ai_tags: List of topic tags.
        full_content: Updated body text (if enriched during analysis).
        image_prompt: Suggested cover-image prompt.
    """
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE news_items
        SET summary = $1, relevance_score = $2, ai_tags = $3::jsonb,
            full_content = COALESCE($4, full_content),
            image_prompt = $5, status = 'ANALYZED', updated_at = NOW()
        WHERE id = $6
        """,
        summary,
        relevance_score,
        json.dumps(ai_tags),
        full_content,
        image_prompt,
        item_id,
    )


async def get_analyzed_items(
    min_score: int = 7,
    hours: int = 24,
) -> list[asyncpg.Record]:
    """Fetch analyzed items above a relevance threshold within a time window.

    Args:
        min_score: Minimum ``relevance_score`` to include.
        hours: Lookback window in hours from now.

    Returns:
        Records ordered by ``relevance_score`` descending.
    """
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT * FROM news_items
        WHERE status = 'ANALYZED'
          AND relevance_score >= $1
          AND published_at > NOW() - make_interval(hours => $2)
        ORDER BY relevance_score DESC
        """,
        min_score,
        hours,
    )


async def mark_items_published(ids: list[UUID]) -> None:
    """Transition a batch of items to ``PUBLISHED`` status.

    Args:
        ids: UUIDs of the items to mark.
    """
    if not ids:
        return
    pool = await get_pool()
    await pool.execute(
        "UPDATE news_items SET status = 'PUBLISHED', updated_at = NOW() WHERE id = ANY($1::uuid[])",
        ids,
    )


# ---------------------------------------------------------------------------
# Briefings
# ---------------------------------------------------------------------------


async def insert_briefing(
    content_markdown: str,
    content_html: Optional[str],
    cover_image_url: Optional[str],
    cover_image_prompt: Optional[str],
    item_ids: list[UUID],
) -> UUID:
    """Insert a new briefing in ``DRAFT`` status.

    Args:
        content_markdown: The briefing body in Markdown.
        content_html: Optional HTML version.
        cover_image_url: URL of the generated cover image.
        cover_image_prompt: Prompt used to generate the cover.
        item_ids: UUIDs of items included in this briefing.

    Returns:
        The UUID of the created briefing.
    """
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO briefings (content_markdown, content_html, cover_image_url, cover_image_prompt, item_ids)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id
        """,
        content_markdown,
        content_html,
        cover_image_url,
        cover_image_prompt,
        item_ids,
    )
    return row["id"]


async def get_recent_briefings(limit: int = 5) -> list[asyncpg.Record]:
    """Return recent distributed briefings for style-memory context."""
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT id, created_at, content_markdown
        FROM briefings
        WHERE status = 'DISTRIBUTED'
        ORDER BY created_at DESC
        LIMIT $1
        """,
        limit,
    )


async def get_latest_briefing() -> Optional[asyncpg.Record]:
    """Return the most recent ``DRAFT`` briefing, or ``None``."""
    pool = await get_pool()
    return await pool.fetchrow(
        "SELECT * FROM briefings WHERE status = 'DRAFT' ORDER BY created_at DESC LIMIT 1"
    )


async def mark_briefing_distributed(
    briefing_id: UUID,
    channels: dict[str, str],
) -> None:
    """Mark a briefing as ``DISTRIBUTED`` and record channel results.

    Args:
        briefing_id: Primary key of the briefing.
        channels: Mapping of channel name to outcome (e.g. ``{"telegram": "ok"}``).
    """
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE briefings
        SET status = 'DISTRIBUTED', distributed_at = NOW(), distribution_channels = $1::jsonb, updated_at = NOW()
        WHERE id = $2
        """,
        json.dumps(channels),
        briefing_id,
    )
