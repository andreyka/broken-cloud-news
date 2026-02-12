from __future__ import annotations

import json
import logging
from typing import Optional
from uuid import UUID

import asyncpg

from bcn.config import Settings

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None


async def get_pool(settings: Optional[Settings] = None) -> asyncpg.Pool:
    global _pool
    if _pool is None:
        s = settings or Settings()
        _pool = await asyncpg.create_pool(s.database_url, min_size=2, max_size=10)
    return _pool


async def close_pool() -> None:
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
    published_at: str,
    raw_data: dict,
    full_content: Optional[str] = None,
) -> Optional[UUID]:
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
        published_at,
        json.dumps(raw_data),
        full_content,
    )
    return row["id"] if row else None


async def get_new_items() -> list[asyncpg.Record]:
    pool = await get_pool()
    return await pool.fetch("SELECT * FROM news_items WHERE status = 'NEW'")


async def update_item_scraped(item_id: UUID, full_content: str) -> None:
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
    juiciness_score: int,
    ai_tags: list[str],
    full_content: Optional[str],
    image_prompt: Optional[str],
) -> None:
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE news_items
        SET summary = $1, juiciness_score = $2, ai_tags = $3::jsonb,
            full_content = COALESCE($4, full_content),
            image_prompt = $5, status = 'ANALYZED', updated_at = NOW()
        WHERE id = $6
        """,
        summary,
        juiciness_score,
        json.dumps(ai_tags),
        full_content,
        image_prompt,
        item_id,
    )


async def get_analyzed_items(min_score: int = 7, hours: int = 24) -> list[asyncpg.Record]:
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT * FROM news_items
        WHERE status = 'ANALYZED'
          AND juiciness_score >= $1
          AND published_at > NOW() - make_interval(hours => $2)
        ORDER BY juiciness_score DESC
        """,
        min_score,
        hours,
    )


async def mark_items_published(ids: list[UUID]) -> None:
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


async def get_latest_briefing() -> Optional[asyncpg.Record]:
    pool = await get_pool()
    return await pool.fetchrow(
        "SELECT * FROM briefings WHERE status = 'DRAFT' ORDER BY created_at DESC LIMIT 1"
    )


async def mark_briefing_distributed(briefing_id: UUID, channels: dict) -> None:
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
