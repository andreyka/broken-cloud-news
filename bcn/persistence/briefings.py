"""Persistence gateway for briefing and distribution-claim state."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
import json
from typing import Optional
from uuid import UUID

import asyncpg

from bcn.persistence.helpers import briefing_item_ids_sql
from bcn.persistence.helpers import dedupe_item_ids
from bcn.persistence.helpers import normalize_retry_error
from bcn.persistence.helpers import normalize_retry_policy
from bcn.persistence.runtime import ensure_schema_ready
from bcn.persistence.runtime import get_pool

_DISTRIBUTION_TERMINAL_STATUS = "DISTRIBUTION_FAILED"


async def ensure_briefing_items_table() -> None:
    """Ensure schema migrations already created briefing tables."""
    await ensure_schema_ready()


async def insert_briefing(
    content_markdown: str,
    content_html: Optional[str],
    cover_image_url: Optional[str],
    cover_image_prompt: Optional[str],
    item_ids: list[UUID],
) -> UUID:
    """Insert a new briefing in ``DRAFT`` status."""
    await ensure_briefing_items_table()
    deduped_item_ids = dedupe_item_ids(item_ids)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO briefings (
                    content_markdown,
                    content_html,
                    cover_image_url,
                    cover_image_prompt
                )
                VALUES ($1, $2, $3, $4)
                RETURNING id
                """,
                content_markdown,
                content_html,
                cover_image_url,
                cover_image_prompt,
            )
            briefing_id = row["id"]

            if deduped_item_ids:
                await conn.executemany(
                    """
                    INSERT INTO briefing_items (
                        briefing_id,
                        news_item_id,
                        position,
                        role
                    )
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (briefing_id, news_item_id) DO UPDATE
                    SET
                        position = EXCLUDED.position,
                        role = EXCLUDED.role
                    """,
                    [
                        (briefing_id, item_id, pos, "selected")
                        for pos, item_id in enumerate(deduped_item_ids)
                    ],
                )

    return briefing_id


async def get_recent_briefings(limit: int = 5) -> list[asyncpg.Record]:
    """Return recent distributed briefings for style-memory context."""
    await ensure_briefing_items_table()
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT id, created_at, content_markdown
        FROM (
            SELECT id, created_at, content_markdown
            FROM briefings
            WHERE status = 'DISTRIBUTED'
            UNION ALL
            SELECT id, posted_at AS created_at, content_markdown
            FROM published_history_posts
        ) AS merged
        ORDER BY created_at DESC
        LIMIT $1
        """,
        limit,
    )


async def get_distributed_briefings(
    *,
    limit: int = 30,
    since_days: int = 0,
) -> list[asyncpg.Record]:
    """Return distributed briefings for simulation/backtest workflows."""
    await ensure_briefing_items_table()
    pool = await get_pool()

    item_ids_sql = briefing_item_ids_sql("b")
    where = ["b.status = 'DISTRIBUTED'"]
    params: list[object] = []

    if since_days > 0:
        params.append(int(since_days))
        where.append(f"b.created_at > NOW() - make_interval(days => ${len(params)})")

    sql = (
        "SELECT "
        "b.id, b.created_at, b.distributed_at, b.content_markdown, "
        f"{item_ids_sql} AS item_ids "
        "FROM briefings b "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY b.created_at DESC"
    )
    if limit > 0:
        params.append(int(limit))
        sql += f" LIMIT ${len(params)}"

    return await pool.fetch(sql, *params)


async def get_latest_any_briefing() -> Optional[asyncpg.Record]:
    """Return the latest briefing regardless of status."""
    await ensure_briefing_items_table()
    item_ids_sql = briefing_item_ids_sql("b")
    pool = await get_pool()
    return await pool.fetchrow(
        f"""
        SELECT
            b.id,
            b.created_at,
            b.cover_image_url,
            b.cover_image_prompt,
            b.content_markdown,
            b.content_html,
            {item_ids_sql} AS item_ids,
            b.status,
            b.distributed_at,
            b.distribution_channels,
            b.updated_at
        FROM briefings b
        ORDER BY b.created_at DESC
        LIMIT 1
        """
    )


async def get_latest_briefing() -> Optional[asyncpg.Record]:
    """Return the most recent ``DRAFT`` briefing, or ``None``."""
    await ensure_briefing_items_table()
    item_ids_sql = briefing_item_ids_sql("b")
    pool = await get_pool()
    return await pool.fetchrow(
        f"""
        SELECT
            b.id,
            b.created_at,
            b.cover_image_url,
            b.cover_image_prompt,
            b.content_markdown,
            b.content_html,
            {item_ids_sql} AS item_ids,
            b.status,
            b.distributed_at,
            b.distribution_channels,
            b.updated_at
        FROM briefings b
        WHERE b.status = 'DRAFT'
        ORDER BY b.created_at DESC
        LIMIT 1
        """
    )


async def claim_latest_draft_briefing(
    *,
    stale_distributing_minutes: int = 30,
    max_distribution_retries: int = 6,
) -> Optional[asyncpg.Record]:
    """Atomically claim the latest draft for distribution."""
    stale_minutes = max(1, int(stale_distributing_minutes))
    retry_limit = max(1, int(max_distribution_retries))
    await ensure_briefing_items_table()
    item_ids_sql = briefing_item_ids_sql("b")
    pool = await get_pool()
    return await pool.fetchrow(
        f"""
        WITH terminalized AS (
            UPDATE briefings
            SET status = 'FAILED',
                terminal_status = $3,
                last_error = COALESCE(last_error, 'distribution_retry_exhausted_stale_claim'),
                next_retry_at = NULL,
                updated_at = NOW()
            WHERE status = 'DISTRIBUTING'
              AND updated_at < NOW() - make_interval(mins => $1)
              AND COALESCE(retry_count, 0) >= $2
              AND terminal_status IS NULL
            RETURNING id
        ),
        candidate AS (
            SELECT id
            FROM briefings
            WHERE (
                    (
                        status = 'DRAFT'
                        AND (next_retry_at IS NULL OR next_retry_at <= NOW())
                    )
                    OR (
                        status = 'DISTRIBUTING'
                        AND updated_at < NOW() - make_interval(mins => $1)
                        AND COALESCE(retry_count, 0) < $2
                    )
                  )
              AND terminal_status IS NULL
            ORDER BY created_at DESC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        ),
        claimed AS (
            UPDATE briefings AS b
            SET status = 'DISTRIBUTING',
                retry_count = CASE
                    WHEN b.status = 'DISTRIBUTING' THEN COALESCE(b.retry_count, 0) + 1
                    ELSE COALESCE(b.retry_count, 0)
                END,
                last_error = CASE
                    WHEN b.status = 'DISTRIBUTING' THEN COALESCE(b.last_error, 'distribution_claim_timeout_reclaimed')
                    ELSE b.last_error
                END,
                next_retry_at = NULL,
                updated_at = NOW()
            FROM candidate
            WHERE b.id = candidate.id
            RETURNING b.id
        )
        SELECT
            b.id,
            b.created_at,
            b.cover_image_url,
            b.cover_image_prompt,
            b.content_markdown,
            b.content_html,
            {item_ids_sql} AS item_ids,
            b.status,
            b.distributed_at,
            b.distribution_channels,
            b.updated_at
        FROM briefings b
        JOIN claimed c ON c.id = b.id
        LIMIT 1
        """,
        stale_minutes,
        retry_limit,
        _DISTRIBUTION_TERMINAL_STATUS,
    )


async def claim_draft_briefing_by_id(
    briefing_id: UUID,
    *,
    stale_distributing_minutes: int = 30,
    max_distribution_retries: int = 6,
) -> Optional[asyncpg.Record]:
    """Atomically claim a specific draft briefing for distribution."""
    stale_minutes = max(1, int(stale_distributing_minutes))
    retry_limit = max(1, int(max_distribution_retries))
    await ensure_briefing_items_table()
    item_ids_sql = briefing_item_ids_sql("b")
    pool = await get_pool()
    return await pool.fetchrow(
        f"""
        WITH terminalized AS (
            UPDATE briefings
            SET status = 'FAILED',
                terminal_status = $4,
                last_error = COALESCE(last_error, 'distribution_retry_exhausted_stale_claim'),
                next_retry_at = NULL,
                updated_at = NOW()
            WHERE id = $1
              AND status = 'DISTRIBUTING'
              AND updated_at < NOW() - make_interval(mins => $2)
              AND COALESCE(retry_count, 0) >= $3
              AND terminal_status IS NULL
            RETURNING id
        ),
        claimed AS (
            UPDATE briefings AS b
            SET status = 'DISTRIBUTING',
                retry_count = CASE
                    WHEN b.status = 'DISTRIBUTING' THEN COALESCE(b.retry_count, 0) + 1
                    ELSE COALESCE(b.retry_count, 0)
                END,
                last_error = CASE
                    WHEN b.status = 'DISTRIBUTING' THEN COALESCE(b.last_error, 'distribution_claim_timeout_reclaimed')
                    ELSE b.last_error
                END,
                next_retry_at = NULL,
                updated_at = NOW()
            WHERE b.id = $1
              AND (
                (
                    b.status = 'DRAFT'
                    AND (b.next_retry_at IS NULL OR b.next_retry_at <= NOW())
                )
                OR (
                    b.status = 'DISTRIBUTING'
                    AND b.updated_at < NOW() - make_interval(mins => $2)
                    AND COALESCE(b.retry_count, 0) < $3
                )
              )
              AND b.terminal_status IS NULL
            RETURNING b.id
        )
        SELECT
            b.id,
            b.created_at,
            b.cover_image_url,
            b.cover_image_prompt,
            b.content_markdown,
            b.content_html,
            {item_ids_sql} AS item_ids,
            b.status,
            b.distributed_at,
            b.distribution_channels,
            b.updated_at
        FROM briefings b
        JOIN claimed c ON c.id = b.id
        LIMIT 1
        """,
        briefing_id,
        stale_minutes,
        retry_limit,
        _DISTRIBUTION_TERMINAL_STATUS,
    )


async def release_briefing_for_retry(
    briefing_id: UUID,
    *,
    error: str | None = None,
    max_retries: int = 6,
    base_delay_seconds: int = 600,
    max_delay_seconds: int = 21600,
) -> None:
    """Release a ``DISTRIBUTING`` briefing using bounded retry metadata."""
    retry_limit, base_delay, max_delay = normalize_retry_policy(
        max_retries=max_retries,
        base_delay_seconds=base_delay_seconds,
        max_delay_seconds=max_delay_seconds,
    )
    error_text = normalize_retry_error(error, fallback="distribution_failed")
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE briefings
        SET retry_count = COALESCE(retry_count, 0) + 1,
            last_error = $2,
            next_retry_at = CASE
                WHEN COALESCE(retry_count, 0) + 1 >= $3 THEN NULL
                ELSE NOW()
                    + make_interval(
                        secs => LEAST(
                            $5,
                            GREATEST(
                                1,
                                $4 * CAST(POWER(2::numeric, GREATEST(COALESCE(retry_count, 0), 0)) AS integer)
                            )
                        )
                    )
            END,
            terminal_status = CASE
                WHEN COALESCE(retry_count, 0) + 1 >= $3 THEN $6
                ELSE NULL
            END,
            status = CASE
                WHEN COALESCE(retry_count, 0) + 1 >= $3 THEN 'FAILED'
                ELSE 'DRAFT'
            END,
            updated_at = NOW()
        WHERE id = $1 AND status = 'DISTRIBUTING'
        """,
        briefing_id,
        error_text,
        retry_limit,
        base_delay,
        max_delay,
        _DISTRIBUTION_TERMINAL_STATUS,
    )


async def get_briefing_by_id(briefing_id: UUID) -> Optional[asyncpg.Record]:
    """Fetch one briefing by UUID."""
    await ensure_briefing_items_table()
    item_ids_sql = briefing_item_ids_sql("b")
    pool = await get_pool()
    return await pool.fetchrow(
        f"""
        SELECT
            b.id,
            b.created_at,
            b.cover_image_url,
            b.cover_image_prompt,
            b.content_markdown,
            b.content_html,
            {item_ids_sql} AS item_ids,
            b.status,
            b.distributed_at,
            b.distribution_channels,
            b.updated_at
        FROM briefings b
        WHERE b.id = $1
        LIMIT 1
        """,
        briefing_id,
    )


async def mark_briefing_distributed(
    briefing_id: UUID,
    channels: dict[str, str],
) -> None:
    """Mark a briefing as ``DISTRIBUTED`` and record channel results."""
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE briefings
        SET status = 'DISTRIBUTED',
            distributed_at = NOW(),
            distribution_channels = $1::jsonb,
            retry_count = 0,
            last_error = NULL,
            next_retry_at = NULL,
            terminal_status = NULL,
            updated_at = NOW()
        WHERE id = $2
        """,
        json.dumps(channels),
        briefing_id,
    )
