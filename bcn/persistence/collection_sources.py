"""Persistence gateway for collection source registry and review state."""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from bcn.persistence.runtime import ensure_schema_ready
from bcn.persistence.runtime import get_pool


async def ensure_collection_source_tables() -> None:
    """Ensure schema migrations already created collection source tables."""
    await ensure_schema_ready()


async def get_collection_source(source_key: str) -> asyncpg.Record | None:
    """Fetch one persisted collection source registry row."""
    await ensure_collection_source_tables()
    pool = await get_pool()
    return await pool.fetchrow(
        """
        SELECT *
        FROM collection_sources
        WHERE source_key = $1
        LIMIT 1
        """,
        source_key,
    )


async def upsert_collection_source(
    *,
    source_key: str,
    source_type: str,
    display_name: str,
    state: str,
    raw_config: dict[str, Any] | None = None,
    review_reason: str | None = None,
    review_payload: dict[str, Any] | None = None,
) -> None:
    """Create or update one collection source registry row."""
    await ensure_collection_source_tables()
    pool = await get_pool()
    payload = json.dumps(review_payload or {}, ensure_ascii=False, default=str)
    config = json.dumps(raw_config or {}, ensure_ascii=False, default=str)
    await pool.execute(
        """
        INSERT INTO collection_sources (
            source_key,
            source_type,
            display_name,
            state,
            raw_config,
            review_reason,
            review_payload,
            first_active_at,
            last_seen_at,
            updated_at
        )
        VALUES (
            $1::text,
            $2::varchar(32),
            $3::text,
            $4::varchar(20),
            $5::jsonb,
            $6::text,
            $7::jsonb,
            CASE WHEN $4::varchar(20) = 'ACTIVE' THEN NOW() ELSE NULL END,
            NOW(),
            NOW()
        )
        ON CONFLICT (source_key) DO UPDATE
        SET
            source_type = EXCLUDED.source_type,
            display_name = EXCLUDED.display_name,
            state = EXCLUDED.state,
            raw_config = EXCLUDED.raw_config,
            review_reason = COALESCE(EXCLUDED.review_reason, collection_sources.review_reason),
            review_payload = CASE
                WHEN EXCLUDED.review_payload <> '{}'::jsonb
                    THEN EXCLUDED.review_payload
                ELSE collection_sources.review_payload
            END,
            first_active_at = CASE
                WHEN collection_sources.first_active_at IS NOT NULL THEN collection_sources.first_active_at
                WHEN EXCLUDED.state = 'ACTIVE' THEN NOW()
                ELSE NULL
            END,
            last_seen_at = NOW(),
            updated_at = NOW()
        """,
        source_key,
        source_type,
        display_name,
        state,
        config,
        review_reason,
        payload,
    )


async def record_collection_source_review(
    *,
    source_key: str,
    decision: str,
    confidence: str,
    rationale: str,
    review_payload: dict[str, Any] | None = None,
) -> None:
    """Persist one source promotion/quarantine review artifact."""
    await ensure_collection_source_tables()
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO collection_source_reviews (
            source_key,
            decision,
            confidence,
            rationale,
            review_payload
        )
        VALUES ($1, $2, $3, $4, $5::jsonb)
        """,
        source_key,
        decision,
        confidence,
        rationale,
        json.dumps(review_payload or {}, ensure_ascii=False, default=str),
    )


async def collection_source_has_historical_items(
    *,
    source_type: str,
    raw_config: dict[str, Any],
) -> bool:
    """Return whether a source already existed before registry enforcement."""
    await ensure_collection_source_tables()
    pool = await get_pool()

    if source_type == "rss":
        feed_url = str(raw_config.get("feed_url") or "").strip()
        if not feed_url:
            return False
        return bool(
            await pool.fetchval(
                """
                SELECT 1
                FROM news_items
                WHERE source_type = 'rss'
                  AND raw_data->>'feed_url' = $1
                LIMIT 1
                """,
                feed_url,
            )
        )

    if source_type == "reddit":
        subreddit = str(raw_config.get("subreddit") or "").strip().lower()
        if not subreddit:
            return False
        return bool(
            await pool.fetchval(
                """
                SELECT 1
                FROM news_items
                WHERE source_type = 'reddit'
                  AND lower(COALESCE(raw_data->>'subreddit', '')) = $1
                LIMIT 1
                """,
                subreddit,
            )
        )

    if source_type == "twitter":
        username = str(raw_config.get("username") or "").strip().lower()
        if not username:
            return False
        return bool(
            await pool.fetchval(
                """
                SELECT 1
                FROM news_items
                WHERE source_type = 'twitter'
                  AND lower(COALESCE(raw_data->>'username', '')) = $1
                LIMIT 1
                """,
                username,
            )
        )

    if source_type == "ghsa":
        return bool(
            await pool.fetchval(
                """
                SELECT 1
                FROM news_items
                WHERE source_type = 'ghsa'
                LIMIT 1
                """
            )
        )

    return False
