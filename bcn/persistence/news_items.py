"""Persistence gateway for news item lifecycle and story identity state."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from email.utils import parsedate_to_datetime
import json
import logging
from typing import Optional
from uuid import UUID

import asyncpg

from bcn.briefing.story_identity import primary_story_issue_key
from bcn.briefing.story_identity import story_url_key
from bcn.persistence.helpers import normalize_retry_error
from bcn.persistence.helpers import normalize_retry_policy
from bcn.persistence.runtime import ensure_schema_ready
from bcn.persistence.runtime import get_pool

logger = logging.getLogger(__name__)
_ANALYSIS_TERMINAL_STATUS = "ANALYSIS_FAILED"
_WRITING_TERMINAL_STATUS = "WRITING_FAILED"
_MAX_FUTURE_PUBLISHED_AT_SKEW = timedelta(hours=6)


async def ensure_news_items_indexes() -> None:
    """Ensure schema migrations already created news_items indexes."""
    await ensure_schema_ready()


def _story_identity_values(
    *,
    url: str,
    title: str | None,
    summary: str | None,
) -> tuple[str | None, str | None]:
    """Compute persisted story identity fields for DB-level dedupe."""
    url_key = story_url_key(url or "") or None
    issue_key = primary_story_issue_key(title or "", summary or "") or None
    return url_key, issue_key


async def _backfill_recent_story_identity(
    *,
    limit: int = 500,
    lookback_days: int = 90,
) -> None:
    """Populate story identity for recent rows created before new columns existed."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT id, url, title, summary
        FROM news_items
        WHERE (
                story_url_key IS NULL
                OR (
                    story_issue_key IS NULL
                    AND (COALESCE(title, '') || ' ' || COALESCE(summary, '')) ~* '(cve-[0-9]{4}-[0-9]+|ghsa-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4})'
                )
              )
          AND published_at > NOW() - make_interval(days => $1)
        ORDER BY published_at DESC
        LIMIT $2
        """,
        max(1, int(lookback_days)),
        max(1, int(limit)),
    )
    if not rows:
        return

    updates: list[tuple[str | None, str | None, UUID]] = []
    for row in rows:
        story_url, story_issue = _story_identity_values(
            url=str(row["url"] or ""),
            title=str(row["title"] or ""),
            summary=str(row["summary"] or ""),
        )
        if story_url is None and story_issue is None:
            continue
        updates.append((story_url, story_issue, row["id"]))

    if not updates:
        return

    await pool.executemany(
        """
        UPDATE news_items
        SET story_url_key = COALESCE($1, story_url_key),
            story_issue_key = COALESCE($2, story_issue_key)
        WHERE id = $3
        """,
        updates,
    )


async def insert_news_item(
    source_type: str,
    source_id: str,
    url: str,
    title: Optional[str],
    published_at: str | datetime,
    raw_data: dict,
    full_content: Optional[str] = None,
) -> Optional[UUID]:
    """Insert a news item and skip duplicates or invalid timestamps."""
    if isinstance(published_at, str):
        raw = published_at.strip()
        if raw:
            try:
                pub_dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                try:
                    pub_dt = parsedate_to_datetime(raw)
                except (TypeError, ValueError):
                    logger.warning(
                        "Skipping %s item %s due to invalid published_at: %r",
                        source_type,
                        source_id,
                        published_at,
                    )
                    return None
        else:
            logger.warning(
                "Skipping %s item %s due to empty published_at",
                source_type,
                source_id,
            )
            return None
    else:
        pub_dt = published_at

    if pub_dt.tzinfo is None:
        pub_dt = pub_dt.replace(tzinfo=timezone.utc)
    else:
        pub_dt = pub_dt.astimezone(timezone.utc)

    if pub_dt > datetime.now(timezone.utc) + _MAX_FUTURE_PUBLISHED_AT_SKEW:
        logger.warning(
            "Skipping %s item %s due to future published_at: %s",
            source_type,
            source_id,
            pub_dt.isoformat(),
        )
        return None

    story_url, story_issue = _story_identity_values(
        url=url,
        title=title,
        summary=None,
    )
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO news_items (
            source_type,
            source_id,
            url,
            title,
            published_at,
            raw_data,
            full_content,
            story_url_key,
            story_issue_key,
            status
        )
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, 'NEW')
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
        story_url,
        story_issue,
    )
    return row["id"] if row else None


async def get_new_items(
    *,
    limit: int = 250,
    stale_analyzing_minutes: int = 120,
    max_analysis_retries: int = 5,
) -> list[asyncpg.Record]:
    """Atomically claim ``NEW`` items for analysis."""
    retry_limit = max(1, int(max_analysis_retries))
    await ensure_news_items_indexes()
    pool = await get_pool()
    return await pool.fetch(
        """
        WITH terminalized AS (
            UPDATE news_items
            SET status = 'DISCARDED',
                terminal_status = $4,
                last_error = COALESCE(last_error, 'analysis_retry_exhausted_stale_claim'),
                next_retry_at = NULL,
                updated_at = NOW()
            WHERE status = 'ANALYZING'
              AND updated_at < NOW() - make_interval(mins => $2)
              AND COALESCE(retry_count, 0) >= $3
              AND terminal_status IS NULL
            RETURNING id
        ),
        candidate AS (
            SELECT id
            FROM news_items
            WHERE (
                    status = 'NEW'
                    OR (
                    status = 'ANALYZING'
                    AND updated_at < NOW() - make_interval(mins => $2)
                    AND COALESCE(retry_count, 0) < $3
                    )
                  )
              AND terminal_status IS NULL
              AND (next_retry_at IS NULL OR next_retry_at <= NOW())
            ORDER BY published_at DESC
            FOR UPDATE SKIP LOCKED
            LIMIT $1
        ),
        claimed AS (
            UPDATE news_items AS n
            SET status = 'ANALYZING',
                retry_count = CASE
                    WHEN n.status = 'ANALYZING' THEN COALESCE(n.retry_count, 0) + 1
                    ELSE COALESCE(n.retry_count, 0)
                END,
                last_error = CASE
                    WHEN n.status = 'ANALYZING' THEN COALESCE(n.last_error, 'analysis_claim_timeout_reclaimed')
                    ELSE n.last_error
                END,
                next_retry_at = NULL,
                updated_at = NOW()
            FROM candidate
            WHERE n.id = candidate.id
            RETURNING n.*
        )
        SELECT *
        FROM claimed
        ORDER BY published_at DESC
        """,
        max(1, int(limit)),
        max(1, int(stale_analyzing_minutes)),
        retry_limit,
        _ANALYSIS_TERMINAL_STATUS,
    )


async def update_item_scraped(item_id: UUID, full_content: str) -> None:
    """Mark an item as ``SCRAPED`` and store the scraped body text."""
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


async def release_items_from_analyzing(
    ids: list[UUID],
    *,
    error: str | None = None,
    max_retries: int = 5,
    base_delay_seconds: int = 300,
    max_delay_seconds: int = 7200,
) -> None:
    """Release claimed ``ANALYZING`` items using bounded retry metadata."""
    if not ids:
        return
    retry_limit, base_delay, max_delay = normalize_retry_policy(
        max_retries=max_retries,
        base_delay_seconds=base_delay_seconds,
        max_delay_seconds=max_delay_seconds,
    )
    error_text = normalize_retry_error(error, fallback="analysis_failed")
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE news_items
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
                WHEN COALESCE(retry_count, 0) + 1 >= $3 THEN 'DISCARDED'
                ELSE 'NEW'
            END,
            updated_at = NOW()
        WHERE id = ANY($1::uuid[])
          AND status = 'ANALYZING'
          AND terminal_status IS NULL
        """,
        ids,
        error_text,
        retry_limit,
        base_delay,
        max_delay,
        _ANALYSIS_TERMINAL_STATUS,
    )


async def update_item_analyzed(
    item_id: UUID,
    summary: str,
    relevance_score: int,
    ai_tags: list[str],
    full_content: Optional[str],
    image_prompt: Optional[str],
    canonical_url: Optional[str] = None,
) -> None:
    """Mark an item as ``ANALYZED`` and store LLM analysis results."""
    pool = await get_pool()
    current = await pool.fetchrow(
        """
        SELECT url, title
        FROM news_items
        WHERE id = $1
        """,
        item_id,
    )
    resolved_url = str(canonical_url or (current["url"] if current else "") or "")
    current_title = str(current["title"] if current else "")
    story_url, story_issue = _story_identity_values(
        url=resolved_url,
        title=current_title,
        summary=summary,
    )
    await pool.execute(
        """
        UPDATE news_items
        SET summary = $1, relevance_score = $2, ai_tags = $3::jsonb,
            full_content = COALESCE($4, full_content),
            image_prompt = $5, url = COALESCE($6, url), status = 'ANALYZED',
            story_url_key = COALESCE($7, story_url_key),
            story_issue_key = COALESCE($8, story_issue_key),
            retry_count = 0, last_error = NULL, next_retry_at = NULL, terminal_status = NULL,
            updated_at = NOW()
        WHERE id = $9
        """,
        summary,
        relevance_score,
        json.dumps(ai_tags),
        full_content,
        image_prompt,
        canonical_url,
        story_url,
        story_issue,
        item_id,
    )


async def get_analyzed_items(
    min_score: int = 7,
    hours: int = 24,
    *,
    limit: int = 250,
    stale_writing_minutes: int = 180,
    max_writing_retries: int = 4,
) -> list[asyncpg.Record]:
    """Atomically claim analyzed items for writer selection."""
    retry_limit = max(1, int(max_writing_retries))
    await ensure_news_items_indexes()
    await _backfill_recent_story_identity(limit=max(250, int(limit) * 4))
    pool = await get_pool()
    return await pool.fetch(
        """
        WITH terminalized AS (
            UPDATE news_items
            SET status = 'DISCARDED',
                terminal_status = $5,
                last_error = COALESCE(last_error, 'writing_retry_exhausted_stale_claim'),
                next_retry_at = NULL,
                updated_at = NOW()
            WHERE status = 'WRITING'
              AND updated_at < NOW() - make_interval(mins => $3)
              AND COALESCE(retry_count, 0) >= $4
              AND terminal_status IS NULL
            RETURNING id
        ),
        eligible AS (
            SELECT
                n.id,
                n.relevance_score,
                n.published_at,
                ROW_NUMBER() OVER (
                    PARTITION BY COALESCE(NULLIF(n.story_issue_key, ''), NULLIF(n.story_url_key, ''), n.id::text)
                    ORDER BY n.relevance_score DESC, n.published_at DESC
                ) AS story_rank
            FROM news_items AS n
            WHERE (
                    n.status = 'ANALYZED'
                    OR (
                        n.status = 'WRITING'
                        AND n.updated_at < NOW() - make_interval(mins => $3)
                        AND COALESCE(n.retry_count, 0) < $4
                    )
                  )
              AND n.terminal_status IS NULL
              AND (n.next_retry_at IS NULL OR n.next_retry_at <= NOW())
              AND n.relevance_score >= $1
              AND n.published_at > NOW() - make_interval(hours => $2)
              AND NOT EXISTS (
                  SELECT 1
                  FROM briefing_items bi
                  JOIN briefings b ON b.id = bi.briefing_id
                  JOIN news_items published_item ON published_item.id = bi.news_item_id
                  WHERE b.status IN ('DRAFT', 'DISTRIBUTING', 'DISTRIBUTED')
                    AND (
                        published_item.id = n.id
                        OR (
                            n.story_issue_key IS NOT NULL
                            AND n.story_issue_key <> ''
                            AND published_item.story_issue_key = n.story_issue_key
                        )
                        OR (
                            n.story_url_key IS NOT NULL
                            AND n.story_url_key <> ''
                            AND published_item.story_url_key = n.story_url_key
                        )
                    )
              )
        ),
        candidate AS (
            SELECT n.id
            FROM news_items AS n
            JOIN eligible ON eligible.id = n.id
            WHERE eligible.story_rank = 1
            ORDER BY eligible.relevance_score DESC, eligible.published_at DESC
            FOR UPDATE SKIP LOCKED
            LIMIT $6
        ),
        claimed AS (
            UPDATE news_items AS n
            SET status = 'WRITING',
                retry_count = CASE
                    WHEN n.status = 'WRITING' THEN COALESCE(n.retry_count, 0) + 1
                    ELSE COALESCE(n.retry_count, 0)
                END,
                last_error = CASE
                    WHEN n.status = 'WRITING' THEN COALESCE(n.last_error, 'writing_claim_timeout_reclaimed')
                    ELSE n.last_error
                END,
                next_retry_at = NULL,
                updated_at = NOW()
            FROM candidate
            WHERE n.id = candidate.id
            RETURNING n.*
        )
        SELECT *
        FROM claimed
        ORDER BY relevance_score DESC, published_at DESC
        """,
        min_score,
        hours,
        max(1, int(stale_writing_minutes)),
        retry_limit,
        _WRITING_TERMINAL_STATUS,
        max(1, int(limit)),
    )


async def preview_analyzed_items(
    min_score: int = 7,
    hours: int = 24,
    *,
    limit: int = 250,
) -> list[asyncpg.Record]:
    """Read analyzed items for shadow evaluation without claiming them."""
    await ensure_news_items_indexes()
    await _backfill_recent_story_identity(limit=max(250, int(limit) * 4))
    pool = await get_pool()
    return await pool.fetch(
        """
        WITH ranked AS (
            SELECT
                n.*,
                ROW_NUMBER() OVER (
                    PARTITION BY COALESCE(NULLIF(n.story_issue_key, ''), NULLIF(n.story_url_key, ''), n.id::text)
                    ORDER BY n.relevance_score DESC, n.published_at DESC
                ) AS story_rank
            FROM news_items AS n
            WHERE n.status = 'ANALYZED'
              AND n.terminal_status IS NULL
              AND n.relevance_score >= $1
              AND n.published_at > NOW() - make_interval(hours => $2)
              AND NOT EXISTS (
                  SELECT 1
                  FROM briefing_items bi
                  JOIN briefings b ON b.id = bi.briefing_id
                  JOIN news_items published_item ON published_item.id = bi.news_item_id
                  WHERE b.status IN ('DRAFT', 'DISTRIBUTING', 'DISTRIBUTED')
                    AND (
                        published_item.id = n.id
                        OR (
                            n.story_issue_key IS NOT NULL
                            AND n.story_issue_key <> ''
                            AND published_item.story_issue_key = n.story_issue_key
                        )
                        OR (
                            n.story_url_key IS NOT NULL
                            AND n.story_url_key <> ''
                            AND published_item.story_url_key = n.story_url_key
                        )
                    )
              )
        )
        SELECT *
        FROM ranked
        WHERE story_rank = 1
        ORDER BY relevance_score DESC, published_at DESC
        LIMIT $3
        """,
        min_score,
        hours,
        max(1, int(limit)),
    )


async def release_items_from_writing(ids: list[UUID]) -> None:
    """Release claimed ``WRITING`` items back to ``ANALYZED``."""
    if not ids:
        return
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE news_items
        SET status = 'ANALYZED',
            retry_count = 0,
            last_error = NULL,
            next_retry_at = NULL,
            terminal_status = NULL,
            updated_at = NOW()
        WHERE id = ANY($1::uuid[]) AND status = 'WRITING'
        """,
        ids,
    )


async def get_top_items_for_period(
    *,
    days: int = 31,
    min_score: int = 7,
    limit: int = 40,
) -> list[asyncpg.Record]:
    """Fetch high-signal items for broader period newsletters."""
    await ensure_news_items_indexes()
    await _backfill_recent_story_identity(limit=max(250, int(limit) * 4))
    pool = await get_pool()
    return await pool.fetch(
        """
        WITH ranked AS (
            SELECT
                n.*,
                ROW_NUMBER() OVER (
                    PARTITION BY COALESCE(NULLIF(n.story_issue_key, ''), NULLIF(n.story_url_key, ''), n.id::text)
                    ORDER BY n.relevance_score DESC, n.published_at DESC
                ) AS story_rank
            FROM news_items AS n
            WHERE n.status = ANY($1::text[])
              AND n.relevance_score >= $2
              AND n.summary IS NOT NULL
              AND n.published_at > NOW() - make_interval(days => $3)
              AND NOT EXISTS (
                  SELECT 1
                  FROM briefing_items bi
                  JOIN briefings b ON b.id = bi.briefing_id
                  JOIN news_items briefing_item ON briefing_item.id = bi.news_item_id
                  WHERE b.status IN ('DRAFT', 'DISTRIBUTING')
                    AND (
                        briefing_item.id = n.id
                        OR (
                            n.story_issue_key IS NOT NULL
                            AND n.story_issue_key <> ''
                            AND briefing_item.story_issue_key = n.story_issue_key
                        )
                        OR (
                            n.story_url_key IS NOT NULL
                            AND n.story_url_key <> ''
                            AND briefing_item.story_url_key = n.story_url_key
                        )
                    )
              )
        )
        SELECT *
        FROM ranked
        WHERE story_rank = 1
        ORDER BY relevance_score DESC, published_at DESC
        LIMIT $4
        """,
        ["ANALYZED", "PUBLISHED"],
        min_score,
        days,
        limit,
    )


async def mark_items_published(ids: list[UUID]) -> None:
    """Transition a batch of items to ``PUBLISHED`` status."""
    if not ids:
        return
    pool = await get_pool()
    await pool.execute(
        "UPDATE news_items SET status = 'PUBLISHED', updated_at = NOW() WHERE id = ANY($1::uuid[])",
        ids,
    )


async def get_recent_published_items(
    hours: int = 24 * 14,
    limit: int = 250,
) -> list[asyncpg.Record]:
    """Fetch recently distributed items for novelty checks."""
    await ensure_news_items_indexes()
    await _backfill_recent_story_identity(limit=max(250, int(limit) * 2))
    pool = await get_pool()
    return await pool.fetch(
        """
        WITH ranked AS (
            SELECT
                n.id,
                n.source_type,
                n.url,
                n.title,
                n.summary,
                n.ai_tags,
                n.relevance_score,
                n.published_at,
                n.raw_data,
                b.distributed_at,
                ROW_NUMBER() OVER (
                    PARTITION BY COALESCE(NULLIF(n.story_issue_key, ''), NULLIF(n.story_url_key, ''), n.id::text)
                    ORDER BY b.distributed_at DESC, n.published_at DESC
                ) AS story_rank
            FROM briefing_items bi
            JOIN briefings b ON b.id = bi.briefing_id
            JOIN news_items n ON n.id = bi.news_item_id
            WHERE b.status = 'DISTRIBUTED'
              AND b.distributed_at IS NOT NULL
              AND b.distributed_at > NOW() - make_interval(hours => $1)
        )
        SELECT id, source_type, url, title, summary, ai_tags, relevance_score, published_at, raw_data
        FROM ranked
        WHERE story_rank = 1
        ORDER BY distributed_at DESC, published_at DESC
        LIMIT $2
        """,
        hours,
        limit,
    )


async def get_items_by_ids(item_ids: list[UUID]) -> list[asyncpg.Record]:
    """Fetch news items by UUID list preserving database ordering."""
    if not item_ids:
        return []
    pool = await get_pool()
    return await pool.fetch(
        "SELECT * FROM news_items WHERE id = ANY($1::uuid[])",
        item_ids,
    )
