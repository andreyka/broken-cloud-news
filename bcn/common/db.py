"""Async PostgreSQL database layer using asyncpg."""

from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import logging
from typing import Any, Optional
from uuid import UUID

import asyncpg

from bcn.briefing.story_identity import primary_story_issue_key
from bcn.briefing.story_identity import story_url_key
from bcn.common.config import Settings
from bcn.common.migrations import apply_migrations
from bcn.common.migrations import get_migration_status

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None
_pool_lock: asyncio.Lock = asyncio.Lock()
_schema_ready: bool = False
_schema_lock: asyncio.Lock = asyncio.Lock()
_RETRY_ERROR_MAX_LEN = 512
_ANALYSIS_TERMINAL_STATUS = "ANALYSIS_FAILED"
_WRITING_TERMINAL_STATUS = "WRITING_FAILED"
_DISTRIBUTION_TERMINAL_STATUS = "DISTRIBUTION_FAILED"
_MAX_FUTURE_PUBLISHED_AT_SKEW = timedelta(hours=6)


async def _get_or_create_pool(settings: Optional[Settings] = None) -> asyncpg.Pool:
    """Return pool instance without enforcing schema migrations."""
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        if _pool is None:
            s = settings or Settings()
            _pool = await asyncpg.create_pool(s.database_url, min_size=2, max_size=10)
    return _pool


async def ensure_schema_ready(pool: Optional[asyncpg.Pool] = None) -> None:
    """Apply DB migrations once for this process before DB access."""
    global _schema_ready
    if _schema_ready:
        return

    active_pool = pool or await _get_or_create_pool()
    async with _schema_lock:
        if _schema_ready:
            return
        applied = await apply_migrations(active_pool)
        if applied:
            logger.info("Applied DB migrations: %s", ", ".join(applied))
        _schema_ready = True


async def migrate_schema(settings: Optional[Settings] = None) -> list[str]:
    """Apply pending schema migrations and mark schema as ready."""
    global _schema_ready
    pool = await _get_or_create_pool(settings)
    applied = await apply_migrations(pool)
    _schema_ready = True
    return applied


async def get_schema_migration_status(
    settings: Optional[Settings] = None,
) -> list[dict[str, Any]]:
    """Return applied/pending migration status rows."""
    pool = await _get_or_create_pool(settings)
    return await get_migration_status(pool)


async def get_pool(settings: Optional[Settings] = None) -> asyncpg.Pool:
    """Return the shared connection pool, creating it on first call.

    Args:
        settings: Optional settings override. Uses defaults when ``None``.

    Returns:
        The asyncpg connection pool.
    """
    pool = await _get_or_create_pool(settings)
    await ensure_schema_ready(pool=pool)
    return pool


async def close_pool() -> None:
    """Close the shared connection pool if it is open."""
    global _pool
    global _schema_ready
    async with _pool_lock:
        if _pool is not None:
            await _pool.close()
            _pool = None
    _schema_ready = False


def _briefing_item_ids_sql(alias: str = "b") -> str:
    """Return item-id expression derived from briefing_items join rows."""
    return f"""
        COALESCE(
            ARRAY(
                SELECT bi.news_item_id
                FROM briefing_items bi
                WHERE bi.briefing_id = {alias}.id
                ORDER BY bi.position ASC, bi.created_at ASC
            ),
            '{{}}'::uuid[]
        )
    """


def _dedupe_item_ids(item_ids: list[UUID]) -> list[UUID]:
    """De-duplicate item ids while preserving order."""
    deduped: list[UUID] = []
    seen: set[UUID] = set()
    for item_id in item_ids:
        if item_id in seen:
            continue
        seen.add(item_id)
        deduped.append(item_id)
    return deduped


def _normalize_retry_policy(
    *,
    max_retries: int,
    base_delay_seconds: int,
    max_delay_seconds: int,
) -> tuple[int, int, int]:
    """Return safe retry policy values used in SQL backoff calculations."""
    retries = max(1, int(max_retries))
    base_delay = max(1, int(base_delay_seconds))
    max_delay = max(base_delay, int(max_delay_seconds))
    return retries, base_delay, max_delay


def _normalize_retry_error(error: str | None, *, fallback: str) -> str:
    """Normalize and truncate retry error text for stable DB storage."""
    value = str(error or "").strip()
    if not value:
        value = fallback
    return value[:_RETRY_ERROR_MAX_LEN]


async def ensure_news_items_indexes() -> None:
    """Ensure schema migrations already created news_items indexes."""
    await ensure_schema_ready()


async def ensure_briefing_items_table() -> None:
    """Ensure schema migrations already created briefing_items table."""
    await ensure_schema_ready()


async def ensure_collection_source_tables() -> None:
    """Ensure schema migrations already created collection source tables."""
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
    """Populate story identity for recent rows created before the new columns existed."""
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
        published_at: Timestamp string (ISO-8601 or RFC 822) or ``datetime`` object.
        raw_data: Original payload stored as JSONB.
        full_content: Scraped or enriched body text.

    Returns:
        The UUID of the newly inserted row, or ``None`` if it already existed
        or the timestamp is invalid.
    """
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
    """Atomically claim ``NEW`` items for analysis.

    Uses ``FOR UPDATE SKIP LOCKED`` so concurrent analyst workers do not process
    the same items. Stale ``ANALYZING`` rows are automatically reclaimed.
    """
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
    retry_limit, base_delay, max_delay = _normalize_retry_policy(
        max_retries=max_retries,
        base_delay_seconds=base_delay_seconds,
        max_delay_seconds=max_delay_seconds,
    )
    error_text = _normalize_retry_error(error, fallback="analysis_failed")
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
    """Mark an item as ``ANALYZED`` and store LLM analysis results.

    Args:
        item_id: Primary key of the item.
        summary: LLM-generated summary.
        relevance_score: Relevance score (1-10).
        ai_tags: List of topic tags.
        full_content: Updated body text (if enriched during analysis).
        image_prompt: Suggested cover-image prompt.
        canonical_url: Optional resolved primary source URL.
    """
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
    """Atomically claim analyzed items for writer selection.

    Uses ``FOR UPDATE SKIP LOCKED`` so concurrent writer workers do not process
    the same candidate set. Stale ``WRITING`` rows are automatically reclaimed.
    """
    retry_limit = max(1, int(max_writing_retries))
    await ensure_news_items_indexes()
    await ensure_briefing_items_table()
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
    await ensure_briefing_items_table()
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
    """Fetch high-signal items for broader period newsletters.

    Unlike ``get_analyzed_items``, this query includes both ``ANALYZED`` and
    ``PUBLISHED`` rows so monthly newsletters can summarize the most relevant
    items of the full period.
    """
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


async def get_recent_published_items(
    hours: int = 24 * 14,
    limit: int = 250,
) -> list[asyncpg.Record]:
    """Fetch recently published items for novelty checks.

    Args:
        hours: Lookback window in hours.
        limit: Maximum number of items to return.

    Returns:
        Published item records ordered by ``published_at`` descending.
    """
    await ensure_news_items_indexes()
    await _backfill_recent_story_identity(limit=max(250, int(limit) * 2))
    pool = await get_pool()
    return await pool.fetch(
        """
        WITH ranked AS (
            SELECT
                id,
                source_type,
                url,
                title,
                summary,
                ai_tags,
                relevance_score,
                published_at,
                raw_data,
                ROW_NUMBER() OVER (
                    PARTITION BY COALESCE(NULLIF(story_issue_key, ''), NULLIF(story_url_key, ''), id::text)
                    ORDER BY published_at DESC
                ) AS story_rank
            FROM news_items
            WHERE status = 'PUBLISHED'
              AND published_at > NOW() - make_interval(hours => $1)
        )
        SELECT id, source_type, url, title, summary, ai_tags, relevance_score, published_at, raw_data
        FROM ranked
        WHERE story_rank = 1
        ORDER BY published_at DESC
        LIMIT $2
        """,
        hours,
        limit,
    )


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
            $1, $2, $3, $4, $5::jsonb, $6, $7::jsonb,
            CASE WHEN $4 = 'ACTIVE' THEN NOW() ELSE NULL END,
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
            Stored in ``briefing_items`` as source of truth.

    Returns:
        The UUID of the created briefing.
    """
    await ensure_briefing_items_table()
    deduped_item_ids = _dedupe_item_ids(item_ids)
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
    await ensure_history_tables()
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
    """Return distributed briefings for simulation/backtest workflows.

    Args:
        limit: Maximum rows to return. Use ``0`` for no explicit limit.
        since_days: Optional lookback window in days. Use ``0`` for all time.
    """
    await ensure_briefing_items_table()
    pool = await get_pool()

    item_ids_sql = _briefing_item_ids_sql("b")
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
    item_ids_sql = _briefing_item_ids_sql("b")
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
    item_ids_sql = _briefing_item_ids_sql("b")
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
    """Atomically claim the latest draft for distribution.

    Transitions one ``DRAFT`` row to ``DISTRIBUTING`` using ``SKIP LOCKED`` so
    concurrent distributor runs cannot claim the same briefing. Also reclaims
    stale ``DISTRIBUTING`` rows older than 30 minutes (e.g., crashed workers).
    """
    stale_minutes = max(1, int(stale_distributing_minutes))
    retry_limit = max(1, int(max_distribution_retries))
    await ensure_briefing_items_table()
    item_ids_sql = _briefing_item_ids_sql("b")
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
    item_ids_sql = _briefing_item_ids_sql("b")
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
    retry_limit, base_delay, max_delay = _normalize_retry_policy(
        max_retries=max_retries,
        base_delay_seconds=base_delay_seconds,
        max_delay_seconds=max_delay_seconds,
    )
    error_text = _normalize_retry_error(error, fallback="distribution_failed")
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


async def get_items_by_ids(item_ids: list[UUID]) -> list[asyncpg.Record]:
    """Fetch news items by UUID list preserving database ordering."""
    if not item_ids:
        return []
    pool = await get_pool()
    return await pool.fetch(
        "SELECT * FROM news_items WHERE id = ANY($1::uuid[])",
        item_ids,
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


# ---------------------------------------------------------------------------
# Historical Channel Posts
# ---------------------------------------------------------------------------


async def ensure_history_tables() -> None:
    """Ensure schema migrations already created history tables."""
    await ensure_schema_ready()


def _history_url_source_id(url: str) -> str:
    """Build stable unique key for history-url synthetic rows."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _history_title(content_markdown: str) -> str:
    """Derive a short title from post text for synthetic URL rows."""
    for raw_line in (content_markdown or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        title = line.lstrip("🔥📉🕵️🦉").strip(" -*")
        return title[:220] if title else "Historical channel post"
    return "Historical channel post"


async def _upsert_history_url_item(
    conn: asyncpg.Connection,
    *,
    url: str,
    posted_at: datetime,
    channel: str,
    author: str,
    content_hash: str,
    content_markdown: str,
    source_type: str = "history_url",
) -> tuple[UUID, bool]:
    """Upsert one synthetic `news_items` row for a URL present in history."""
    source_id = _history_url_source_id(url)
    title = _history_title(content_markdown)
    payload = {
        "origin": "history_import",
        "channel": channel,
        "author": author,
        "content_hash": content_hash,
        "url": url,
    }
    story_url, story_issue = _story_identity_values(
        url=url,
        title=title,
        summary=None,
    )

    exists = await conn.fetchval(
        """
        SELECT id
        FROM news_items
        WHERE source_type = $1 AND source_id = $2
        LIMIT 1
        """,
        source_type,
        source_id,
    )

    row = await conn.fetchrow(
        """
        INSERT INTO news_items (
            source_type,
            source_id,
            url,
            title,
            published_at,
            raw_data,
            story_url_key,
            story_issue_key,
            status
        )
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, 'PUBLISHED')
        ON CONFLICT (source_type, source_id) DO UPDATE
        SET
            status = 'PUBLISHED',
            published_at = LEAST(news_items.published_at, EXCLUDED.published_at),
            updated_at = NOW(),
            title = COALESCE(news_items.title, EXCLUDED.title),
            raw_data = COALESCE(news_items.raw_data, '{}'::jsonb) || EXCLUDED.raw_data,
            story_url_key = COALESCE(news_items.story_url_key, EXCLUDED.story_url_key),
            story_issue_key = COALESCE(news_items.story_issue_key, EXCLUDED.story_issue_key)
        RETURNING id
        """,
        source_type,
        source_id,
        url,
        title,
        posted_at,
        json.dumps(payload, ensure_ascii=False, default=str),
        story_url,
        story_issue,
    )
    return row["id"], not bool(exists)


async def import_channel_history_posts(
    *,
    channel: str,
    posts: list[dict[str, Any]],
    source_type: str = "history_url",
) -> dict[str, int]:
    """Import previously published channel posts into DB history tables.

    The import is idempotent:
    - posts are deduplicated by ``content_hash`` in ``published_history_posts``
    - referenced URLs are upserted into ``news_items`` by canonical URL hash
    """
    await ensure_history_tables()
    pool = await get_pool()

    inserted_posts = 0
    existing_posts = 0
    inserted_urls = 0
    existing_urls = 0
    skipped_posts = 0

    async with pool.acquire() as conn:
        async with conn.transaction():
            for post in posts:
                content_hash = str(post.get("content_hash") or "").strip().lower()
                content_markdown = str(post.get("content_markdown") or "").strip()
                posted_at = post.get("posted_at")
                if not content_hash or not content_markdown or not isinstance(
                    posted_at, datetime
                ):
                    skipped_posts += 1
                    continue

                author = str(post.get("author") or "").strip()
                urls = [
                    str(url).strip()
                    for url in (post.get("urls") or [])
                    if str(url).strip()
                ]

                history_exists = await conn.fetchval(
                    """
                    SELECT id
                    FROM published_history_posts
                    WHERE content_hash = $1
                    LIMIT 1
                    """,
                    content_hash,
                )
                if history_exists:
                    existing_posts += 1
                    continue

                item_ids: list[UUID] = []
                for url in urls:
                    item_id, is_new = await _upsert_history_url_item(
                        conn,
                        url=url,
                        posted_at=posted_at,
                        channel=channel,
                        author=author,
                        content_hash=content_hash,
                        content_markdown=content_markdown,
                        source_type=source_type,
                    )
                    item_ids.append(item_id)
                    if is_new:
                        inserted_urls += 1
                    else:
                        existing_urls += 1

                await conn.execute(
                    """
                    INSERT INTO published_history_posts (
                        channel,
                        author,
                        posted_at,
                        content_markdown,
                        content_hash,
                        urls,
                        item_ids,
                        metadata
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, $6::jsonb, $7::uuid[], $8::jsonb
                    )
                    """,
                    channel.strip().lower(),
                    author,
                    posted_at,
                    content_markdown,
                    content_hash,
                    json.dumps(urls, ensure_ascii=False),
                    item_ids,
                    json.dumps({"origin": "history_import"}, ensure_ascii=False),
                )
                inserted_posts += 1

    return {
        "inserted_posts": inserted_posts,
        "existing_posts": existing_posts,
        "inserted_urls": inserted_urls,
        "existing_urls": existing_urls,
        "skipped_posts": skipped_posts,
    }


# ---------------------------------------------------------------------------
# Newsletter Subscribers
# ---------------------------------------------------------------------------


def _normalize_subscriber_email(value: str) -> str:
    """Normalize subscriber email for stable storage/lookup."""
    return str(value or "").strip().lower()


async def ensure_newsletter_tables() -> None:
    """Ensure schema migrations already created newsletter tables."""
    await ensure_schema_ready()


async def add_newsletter_subscriber(email: str) -> bool:
    """Add or reactivate one newsletter subscriber email."""
    normalized = _normalize_subscriber_email(email)
    if not normalized or "@" not in normalized:
        raise ValueError("Invalid email address")
    await ensure_newsletter_tables()
    pool = await get_pool()
    status = await pool.execute(
        """
        INSERT INTO newsletter_subscribers (email, is_active, updated_at)
        VALUES ($1, TRUE, NOW())
        ON CONFLICT (email) DO UPDATE
        SET is_active = TRUE, updated_at = NOW()
        """,
        normalized,
    )
    return status.startswith("INSERT")


async def remove_newsletter_subscriber(email: str) -> bool:
    """Soft-delete one newsletter subscriber by deactivating it."""
    normalized = _normalize_subscriber_email(email)
    if not normalized:
        return False
    await ensure_newsletter_tables()
    pool = await get_pool()
    status = await pool.execute(
        """
        UPDATE newsletter_subscribers
        SET is_active = FALSE, updated_at = NOW()
        WHERE email = $1 AND is_active = TRUE
        """,
        normalized,
    )
    return status == "UPDATE 1"


async def get_newsletter_subscribers(*, active_only: bool = True) -> list[asyncpg.Record]:
    """Return newsletter subscribers ordered by email."""
    await ensure_newsletter_tables()
    pool = await get_pool()
    if active_only:
        return await pool.fetch(
            """
            SELECT id, email, is_active, created_at, updated_at
            FROM newsletter_subscribers
            WHERE is_active = TRUE
            ORDER BY email ASC
            """
        )
    return await pool.fetch(
        """
        SELECT id, email, is_active, created_at, updated_at
        FROM newsletter_subscribers
        ORDER BY email ASC
        """
    )


# ---------------------------------------------------------------------------
# Simulation Runs
# ---------------------------------------------------------------------------


def _coerce_iso_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    return None


def _coerce_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_json_dict(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


async def ensure_simulation_tables() -> None:
    """Ensure schema migrations already created simulation tables."""
    await ensure_schema_ready()


async def count_simulation_runs() -> int:
    """Return the number of stored simulation runs."""
    await ensure_simulation_tables()
    pool = await get_pool()
    row = await pool.fetchrow("SELECT COUNT(*)::int AS count FROM simulation_runs")
    return int(row["count"]) if row else 0


async def insert_simulation_report(
    report: dict[str, Any],
    *,
    report_path: str | None = None,
    source: str = "cli",
    notes: str | None = None,
) -> UUID:
    """Persist a simulation report and per-briefing results."""
    await ensure_simulation_tables()
    pool = await get_pool()

    summary = report.get("summary")
    if not isinstance(summary, dict):
        summary = {}

    params = {
        "limit": _coerce_int(report.get("limit"), 0),
        "since_days": _coerce_int(report.get("since_days"), 0),
        "include_text": bool(report.get("include_text", False)),
        "apply_critic_rewrites": bool(report.get("apply_critic_rewrites", False)),
    }
    generated_at = _coerce_iso_datetime(report.get("generated_at"))
    run_count = _coerce_int(report.get("count"), 0)

    run = await pool.fetchrow(
        """
        INSERT INTO simulation_runs (
            generated_at, source, report_path, params, summary, count, notes
        )
        VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6, $7)
        RETURNING id
        """,
        generated_at,
        source,
        report_path,
        json.dumps(params),
        json.dumps(summary),
        run_count,
        notes,
    )
    run_id = run["id"]

    raw_results = report.get("results")
    results = raw_results if isinstance(raw_results, list) else []
    if results:
        payloads: list[
            tuple[UUID, str | None, datetime | None, int, int, int, str]
        ] = []
        for row in results:
            if not isinstance(row, dict):
                continue
            briefing_id_raw = row.get("briefing_id")
            briefing_id = str(briefing_id_raw).strip() if briefing_id_raw else None
            payloads.append(
                (
                    run_id,
                    briefing_id if briefing_id else None,
                    _coerce_iso_datetime(row.get("created_at")),
                    _coerce_int(row.get("actual_score"), 0),
                    _coerce_int(row.get("simulated_score"), 0),
                    _coerce_int(row.get("delta"), 0),
                    json.dumps(row, ensure_ascii=False),
                )
            )

        if payloads:
            await pool.executemany(
                """
                INSERT INTO simulation_results (
                    run_id,
                    briefing_id,
                    briefing_created_at,
                    actual_score,
                    simulated_score,
                    delta,
                    result
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                ON CONFLICT (run_id, briefing_id) DO UPDATE
                SET
                    briefing_created_at = EXCLUDED.briefing_created_at,
                    actual_score = EXCLUDED.actual_score,
                    simulated_score = EXCLUDED.simulated_score,
                    delta = EXCLUDED.delta,
                    result = EXCLUDED.result
                """,
                payloads,
            )

    return run_id


async def get_latest_simulation_run(
    *,
    exclude_run_id: UUID | None = None,
) -> Optional[asyncpg.Record]:
    """Fetch the latest simulation run metadata."""
    await ensure_simulation_tables()
    pool = await get_pool()
    if exclude_run_id:
        return await pool.fetchrow(
            """
            SELECT *
            FROM simulation_runs
            WHERE id <> $1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            exclude_run_id,
        )
    return await pool.fetchrow("""
        SELECT *
        FROM simulation_runs
        ORDER BY created_at DESC
        LIMIT 1
        """)


async def get_simulation_report_by_id(run_id: UUID) -> dict[str, Any] | None:
    """Load a full simulation report object by run id."""
    await ensure_simulation_tables()
    pool = await get_pool()
    run = await pool.fetchrow(
        """
        SELECT id, created_at, generated_at, source, report_path, params, summary, count, notes
        FROM simulation_runs
        WHERE id = $1
        """,
        run_id,
    )
    if not run:
        return None

    rows = await pool.fetch(
        """
        SELECT result
        FROM simulation_results
        WHERE run_id = $1
        ORDER BY briefing_created_at NULLS LAST, created_at ASC
        """,
        run_id,
    )
    results: list[dict[str, Any]] = []
    for row in rows:
        payload = row["result"]
        if isinstance(payload, dict):
            results.append(payload)
            continue
        if isinstance(payload, str):
            try:
                parsed = json.loads(payload)
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed = None
            if isinstance(parsed, dict):
                results.append(parsed)

    params = _coerce_json_dict(run["params"])
    summary = _coerce_json_dict(run["summary"])
    generated_at = run["generated_at"] or run["created_at"]

    report: dict[str, Any] = {
        "generated_at": generated_at.isoformat()
        if isinstance(generated_at, datetime)
        else None,
        "count": int(run["count"]),
        "limit": _coerce_int(params.get("limit"), 0),
        "since_days": _coerce_int(params.get("since_days"), 0),
        "include_text": bool(params.get("include_text", False)),
        "apply_critic_rewrites": bool(params.get("apply_critic_rewrites", False)),
        "summary": summary,
        "results": results,
        "db_run_id": str(run["id"]),
        "db_created_at": run["created_at"].isoformat(),
        "db_source": str(run["source"]),
    }
    if run["report_path"]:
        report["report_path"] = str(run["report_path"])
    if run["notes"]:
        report["notes"] = str(run["notes"])
    return report


async def get_latest_simulation_report(
    *,
    exclude_run_id: UUID | None = None,
) -> dict[str, Any] | None:
    """Load latest simulation report object from DB."""
    run = await get_latest_simulation_run(exclude_run_id=exclude_run_id)
    if not run:
        return None
    return await get_simulation_report_by_id(run["id"])


# ---------------------------------------------------------------------------
# Evaluation Runs
# ---------------------------------------------------------------------------


async def ensure_evaluation_tables() -> None:
    """Ensure schema migrations already created evaluation tables."""
    await ensure_schema_ready()


async def count_evaluation_runs(*, lane: str | None = None) -> int:
    """Return the number of stored evaluation runs."""
    await ensure_evaluation_tables()
    pool = await get_pool()
    if lane:
        row = await pool.fetchrow(
            """
            SELECT COUNT(*)::int AS count
            FROM evaluation_runs
            WHERE lane = $1
            """,
            str(lane),
        )
    else:
        row = await pool.fetchrow(
            """
            SELECT COUNT(*)::int AS count
            FROM evaluation_runs
            """
        )
    return int(row["count"]) if row else 0


async def create_evaluation_run(
    *,
    lane: str,
    source: str = "cli",
    report_path: str | None = None,
    pack_path: str | None = None,
    workflow_mode: str | None = None,
    params: dict[str, Any] | None = None,
    candidate_overrides: dict[str, Any] | None = None,
    notes: str | None = None,
) -> UUID:
    """Create a placeholder evaluation run row before work starts."""
    await ensure_evaluation_tables()
    pool = await get_pool()

    normalized_lane = str(lane or "").strip().lower()
    if normalized_lane not in {"benchmark", "shadow"}:
        raise ValueError("Evaluation run lane must be 'benchmark' or 'shadow'.")

    run = await pool.fetchrow(
        """
        INSERT INTO evaluation_runs (
            lane,
            source,
            report_path,
            pack_path,
            workflow_mode,
            params,
            candidate_overrides,
            summary,
            report,
            count,
            notes,
            status
        )
        VALUES (
            $1,
            $2,
            $3,
            $4,
            $5,
            $6::jsonb,
            $7::jsonb,
            '{}'::jsonb,
            '{}'::jsonb,
            0,
            $8,
            'running'
        )
        RETURNING id
        """,
        normalized_lane,
        source,
        report_path,
        pack_path,
        workflow_mode,
        json.dumps(params or {}, ensure_ascii=False, default=str),
        json.dumps(candidate_overrides or {}, ensure_ascii=False, default=str),
        notes,
    )
    return run["id"]


async def complete_evaluation_run(
    run_id: UUID,
    report: dict[str, Any],
    *,
    report_path: str | None = None,
    notes: str | None = None,
) -> None:
    """Finalize a previously created evaluation run row."""
    await ensure_evaluation_tables()
    pool = await get_pool()

    lane = str(report.get("lane") or "").strip().lower()
    if lane not in {"benchmark", "shadow"}:
        raise ValueError("Evaluation report lane must be 'benchmark' or 'shadow'.")

    summary = report.get("summary")
    if not isinstance(summary, dict):
        summary = {}

    params: dict[str, Any] = {}
    pack_path = None
    workflow_mode = None
    run_count = _coerce_int(report.get("count"), 0)
    if lane == "benchmark":
        pack_path = str(report.get("pack_path") or "").strip() or None
        params["case_count"] = run_count
    else:
        workflow_mode = str(report.get("workflow_mode") or "").strip() or None
        params["item_pool_count"] = _coerce_int(report.get("item_pool_count"), 0)
        run_count = 1

    candidate_overrides = report.get("candidate_overrides")
    if not isinstance(candidate_overrides, dict):
        candidate_overrides = {}

    await pool.execute(
        """
        UPDATE evaluation_runs
        SET
            generated_at = $2,
            report_path = COALESCE($3, report_path),
            pack_path = COALESCE($4, pack_path),
            workflow_mode = COALESCE($5, workflow_mode),
            params = $6::jsonb,
            candidate_overrides = $7::jsonb,
            summary = $8::jsonb,
            report = $9::jsonb,
            count = $10,
            notes = COALESCE($11, notes),
            status = 'completed',
            error_message = NULL,
            finished_at = NOW(),
            updated_at = NOW()
        WHERE id = $1
        """,
        run_id,
        _coerce_iso_datetime(report.get("generated_at")),
        report_path,
        pack_path,
        workflow_mode,
        json.dumps(params, ensure_ascii=False, default=str),
        json.dumps(candidate_overrides, ensure_ascii=False, default=str),
        json.dumps(summary, ensure_ascii=False, default=str),
        json.dumps(report, ensure_ascii=False, default=str),
        run_count,
        notes,
    )


async def fail_evaluation_run(
    run_id: UUID,
    *,
    error_message: str,
    notes: str | None = None,
) -> None:
    """Mark an evaluation run as failed."""
    await ensure_evaluation_tables()
    pool = await get_pool()
    message = (error_message or "").strip()[:4000] or "evaluation_failed"
    summary = {
        "recommendation": "failed",
        "confidence": "low",
    }
    report = {
        "error": message,
    }
    await pool.execute(
        """
        UPDATE evaluation_runs
        SET
            summary = $2::jsonb,
            report = CASE
                WHEN report = '{}'::jsonb THEN $3::jsonb
                ELSE report
            END,
            notes = COALESCE($4, notes),
            status = 'failed',
            error_message = $5,
            finished_at = NOW(),
            updated_at = NOW()
        WHERE id = $1
        """,
        run_id,
        json.dumps(summary, ensure_ascii=False, default=str),
        json.dumps(report, ensure_ascii=False, default=str),
        notes,
        message,
    )


async def insert_evaluation_report(
    report: dict[str, Any],
    *,
    report_path: str | None = None,
    source: str = "cli",
    notes: str | None = None,
) -> UUID:
    """Persist a benchmark or shadow report."""
    await ensure_evaluation_tables()
    pool = await get_pool()

    lane = str(report.get("lane") or "").strip().lower()
    if lane not in {"benchmark", "shadow"}:
        raise ValueError("Evaluation report lane must be 'benchmark' or 'shadow'.")

    summary = report.get("summary")
    if not isinstance(summary, dict):
        summary = {}

    params: dict[str, Any] = {}
    pack_path = None
    workflow_mode = None
    run_count = _coerce_int(report.get("count"), 0)
    if lane == "benchmark":
        pack_path = str(report.get("pack_path") or "").strip() or None
        params["case_count"] = run_count
    else:
        workflow_mode = str(report.get("workflow_mode") or "").strip() or None
        params["item_pool_count"] = _coerce_int(report.get("item_pool_count"), 0)
        run_count = 1

    candidate_overrides = report.get("candidate_overrides")
    if not isinstance(candidate_overrides, dict):
        candidate_overrides = {}

    run = await pool.fetchrow(
        """
        INSERT INTO evaluation_runs (
            generated_at,
            lane,
            source,
            report_path,
            pack_path,
            workflow_mode,
            params,
            candidate_overrides,
            summary,
            report,
            count,
            notes,
            status,
            finished_at
        )
        VALUES (
            $1,
            $2,
            $3,
            $4,
            $5,
            $6,
            $7::jsonb,
            $8::jsonb,
            $9::jsonb,
            $10::jsonb,
            $11,
            $12,
            'completed',
            NOW()
        )
        RETURNING id
        """,
        _coerce_iso_datetime(report.get("generated_at")),
        lane,
        source,
        report_path,
        pack_path,
        workflow_mode,
        json.dumps(params),
        json.dumps(candidate_overrides),
        json.dumps(summary),
        json.dumps(report, ensure_ascii=False, default=str),
        run_count,
        notes,
    )
    return run["id"]


async def get_latest_evaluation_run(
    *,
    lane: str | None = None,
    exclude_run_id: UUID | None = None,
) -> Optional[asyncpg.Record]:
    """Fetch the latest stored evaluation run metadata."""
    await ensure_evaluation_tables()
    pool = await get_pool()

    conditions: list[str] = []
    params: list[Any] = []
    if lane:
        params.append(str(lane))
        conditions.append(f"lane = ${len(params)}")
    if exclude_run_id:
        params.append(exclude_run_id)
        conditions.append(f"id <> ${len(params)}")

    where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"""
        SELECT *
        FROM evaluation_runs
        {where_sql}
        ORDER BY created_at DESC
        LIMIT 1
    """
    return await pool.fetchrow(query, *params)


async def get_evaluation_report_by_id(run_id: UUID) -> dict[str, Any] | None:
    """Load a full evaluation report object by run id."""
    await ensure_evaluation_tables()
    pool = await get_pool()
    run = await pool.fetchrow(
        """
        SELECT
            id,
            created_at,
            generated_at,
            lane,
            source,
            report_path,
            pack_path,
            workflow_mode,
            params,
            candidate_overrides,
            status,
            finished_at,
            error_message,
            summary,
            report,
            count,
            notes
        FROM evaluation_runs
        WHERE id = $1
        """,
        run_id,
    )
    if not run:
        return None

    report = _coerce_json_dict(run["report"])
    if not report:
        report = {
            "generated_at": (
                run["generated_at"].isoformat()
                if isinstance(run["generated_at"], datetime)
                else None
            ),
            "lane": str(run["lane"]),
            "count": int(run["count"]),
            "summary": _coerce_json_dict(run["summary"]),
        }
    report["db_run_id"] = str(run["id"])
    report["db_created_at"] = run["created_at"].isoformat()
    report["db_source"] = str(run["source"])
    report["db_status"] = str(run["status"] or "completed")
    if run["finished_at"]:
        report["db_finished_at"] = run["finished_at"].isoformat()
    if run["error_message"]:
        report["db_error_message"] = str(run["error_message"])
    if run["report_path"]:
        report["report_path"] = str(run["report_path"])
    if run["pack_path"]:
        report["pack_path"] = str(run["pack_path"])
    if run["workflow_mode"]:
        report["workflow_mode"] = str(run["workflow_mode"])
    if run["notes"]:
        report["notes"] = str(run["notes"])
    return report


async def get_latest_evaluation_report(
    *,
    lane: str | None = None,
    exclude_run_id: UUID | None = None,
) -> dict[str, Any] | None:
    """Load the latest evaluation report object from DB."""
    run = await get_latest_evaluation_run(lane=lane, exclude_run_id=exclude_run_id)
    if not run:
        return None
    return await get_evaluation_report_by_id(run["id"])


async def list_recent_evaluation_runs(
    *,
    lane: str | None = None,
    limit: int = 20,
) -> list[asyncpg.Record]:
    """Return recent evaluation runs for CLI and dashboard summaries."""
    await ensure_evaluation_tables()
    pool = await get_pool()
    row_limit = max(1, int(limit))
    if lane:
        return await pool.fetch(
            """
            SELECT
                id,
                created_at,
                generated_at,
                lane,
                source,
                report_path,
                pack_path,
                workflow_mode,
                candidate_overrides,
                status,
                finished_at,
                error_message,
                summary,
                count
            FROM evaluation_runs
            WHERE lane = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            str(lane),
            row_limit,
        )
    return await pool.fetch(
        """
        SELECT
            id,
            created_at,
            generated_at,
            lane,
            source,
            report_path,
            pack_path,
            workflow_mode,
            candidate_overrides,
            status,
            finished_at,
            error_message,
            summary,
            count
        FROM evaluation_runs
        ORDER BY created_at DESC
        LIMIT $1
        """,
        row_limit,
    )


async def get_evaluation_runs_for_export(
    *,
    lane: str = "shadow",
    limit: int = 0,
    since_days: int = 0,
) -> list[asyncpg.Record]:
    """Fetch persisted evaluation runs for downstream dataset export."""
    await ensure_evaluation_tables()
    pool = await get_pool()
    params: list[Any] = [str(lane)]
    where = [f"lane = ${len(params)}"]
    where.append("status = 'completed'")
    if since_days > 0:
        params.append(int(since_days))
        where.append(f"created_at > NOW() - make_interval(days => ${len(params)})")

    sql = (
        """
        SELECT
            id,
            created_at,
            generated_at,
            lane,
            source,
            workflow_mode,
            candidate_overrides,
            summary,
            report
        FROM evaluation_runs
        WHERE
        """
        + " AND ".join(where)
        + """
        ORDER BY created_at DESC
        """
    )
    if limit > 0:
        params.append(max(1, int(limit)))
        sql += f" LIMIT ${len(params)}"
    return await pool.fetch(sql, *params)


# ---------------------------------------------------------------------------
# Fine-Tuning Data: Traces, Preferences, Reviews, Outcomes
# ---------------------------------------------------------------------------


async def ensure_training_tables() -> None:
    """Ensure schema migrations already created training tables."""
    await ensure_schema_ready()


async def create_generation_run(
    *,
    trigger_source: str,
    mode: str,
    selected_item_ids: list[UUID],
    selected_items: list[dict[str, Any]],
    llm_model: str,
    llm_model_version: str | None = None,
    prompts: dict[str, Any] | None = None,
    config_snapshot: dict[str, Any] | None = None,
    git_sha: str | None = None,
    initial_draft: str | None = None,
) -> UUID:
    """Create a generation run entry for full writer trace persistence."""
    await ensure_training_tables()
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO generation_runs (
            trigger_source,
            mode,
            selected_item_ids,
            selected_items,
            llm_model,
            llm_model_version,
            prompts,
            config_snapshot,
            git_sha,
            initial_draft
        )
        VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7::jsonb, $8::jsonb, $9, $10)
        RETURNING id
        """,
        trigger_source,
        mode,
        selected_item_ids,
        json.dumps(selected_items, ensure_ascii=False, default=str),
        llm_model,
        llm_model_version,
        json.dumps(prompts or {}, ensure_ascii=False, default=str),
        json.dumps(config_snapshot or {}, ensure_ascii=False, default=str),
        git_sha,
        initial_draft,
    )
    return row["id"]


async def append_generation_round(
    *,
    run_id: UUID,
    round_index: int,
    phase: str,
    draft_input: str,
    gate_result: dict[str, Any] | None,
    critique_result: dict[str, Any] | None,
    verifier_result: dict[str, Any] | None,
    feedback: list[str] | None,
    rewrite_output: str | None,
    passed: bool,
) -> None:
    """Persist one evaluate/rewrite round artifact for a generation run."""
    await ensure_training_tables()
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO generation_rounds (
            run_id,
            round_index,
            phase,
            draft_input,
            gate_result,
            critique_result,
            verifier_result,
            feedback,
            rewrite_output,
            passed
        )
        VALUES (
            $1,
            $2,
            $3,
            $4,
            $5::jsonb,
            $6::jsonb,
            $7::jsonb,
            $8::jsonb,
            $9,
            $10
        )
        ON CONFLICT (run_id, round_index) DO UPDATE
        SET
            phase = EXCLUDED.phase,
            draft_input = EXCLUDED.draft_input,
            gate_result = EXCLUDED.gate_result,
            critique_result = EXCLUDED.critique_result,
            verifier_result = EXCLUDED.verifier_result,
            feedback = EXCLUDED.feedback,
            rewrite_output = EXCLUDED.rewrite_output,
            passed = EXCLUDED.passed
        """,
        run_id,
        int(round_index),
        phase,
        draft_input,
        json.dumps(gate_result or {}, ensure_ascii=False, default=str),
        json.dumps(critique_result or {}, ensure_ascii=False, default=str),
        json.dumps(verifier_result or {}, ensure_ascii=False, default=str),
        json.dumps(feedback or [], ensure_ascii=False, default=str),
        rewrite_output,
        bool(passed),
    )


async def insert_generation_preference_pair(
    *,
    run_id: UUID,
    round_index: int,
    chosen_text: str,
    rejected_text: str,
    rationale: str | None = None,
    source: str = "auto_writer_loop",
) -> None:
    """Store chosen/rejected pair data for preference ranking/DPO."""
    chosen = (chosen_text or "").strip()
    rejected = (rejected_text or "").strip()
    if not chosen or not rejected:
        return
    if chosen == rejected:
        return
    await ensure_training_tables()
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO generation_preference_pairs (
            run_id,
            round_index,
            source,
            chosen_text,
            rejected_text,
            rationale
        )
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        run_id,
        int(round_index),
        source,
        chosen,
        rejected,
        rationale,
    )


async def finalize_generation_run(
    *,
    run_id: UUID,
    decision: str,
    decision_reason: str | None,
    rewrite_count: int,
    final_draft: str | None,
    final_gate: dict[str, Any] | None,
    final_critique: dict[str, Any] | None,
    final_verifier: dict[str, Any] | None,
    briefing_id: UUID | None = None,
) -> None:
    """Finalize run metadata once writer publishes or blocks a draft."""
    await ensure_training_tables()
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE generation_runs
        SET
            updated_at = NOW(),
            decision = $1,
            decision_reason = $2,
            rewrite_count = $3,
            final_draft = $4,
            final_gate = $5::jsonb,
            final_critique = $6::jsonb,
            final_verifier = $7::jsonb,
            briefing_id = COALESCE($8, briefing_id)
        WHERE id = $9
        """,
        (decision or "PENDING").strip().upper(),
        decision_reason,
        int(rewrite_count),
        final_draft,
        json.dumps(final_gate or {}, ensure_ascii=False, default=str),
        json.dumps(final_critique or {}, ensure_ascii=False, default=str),
        json.dumps(final_verifier or {}, ensure_ascii=False, default=str),
        briefing_id,
        run_id,
    )


async def finalize_stale_pending_generation_runs(
    *,
    max_age_minutes: int = 180,
    decision: str = "BLOCKED",
    decision_reason: str = "auto_finalized_stale_pending_run",
) -> int:
    """Finalize stale ``PENDING`` generation runs to avoid dangling traces."""
    await ensure_training_tables()
    pool = await get_pool()
    threshold = max(1, int(max_age_minutes))
    rows = await pool.fetch(
        """
        UPDATE generation_runs
        SET
            updated_at = NOW(),
            decision = $1,
            decision_reason = COALESCE(NULLIF(decision_reason, ''), $2),
            final_draft = COALESCE(NULLIF(final_draft, ''), initial_draft)
        WHERE decision = 'PENDING'
          AND created_at < NOW() - make_interval(mins => $3)
        RETURNING id
        """,
        (decision or "BLOCKED").strip().upper(),
        decision_reason,
        threshold,
    )
    return len(rows)


async def get_latest_generation_run_for_briefing(
    briefing_id: UUID,
) -> Optional[asyncpg.Record]:
    """Return latest trace run linked to a given briefing."""
    await ensure_training_tables()
    pool = await get_pool()
    return await pool.fetchrow(
        """
        SELECT *
        FROM generation_runs
        WHERE briefing_id = $1
        ORDER BY created_at DESC
        LIMIT 1
        """,
        briefing_id,
    )


async def get_briefing_by_id(briefing_id: UUID) -> Optional[asyncpg.Record]:
    """Fetch one briefing by UUID."""
    await ensure_briefing_items_table()
    item_ids_sql = _briefing_item_ids_sql("b")
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


async def insert_human_review(
    *,
    briefing_id: UUID,
    decision: str,
    issue_tags: list[str] | None = None,
    reviewer: str = "cli",
    edited_markdown: str | None = None,
    notes: str | None = None,
    run_id: UUID | None = None,
) -> UUID:
    """Store manual review labels/edits for a briefing."""
    await ensure_training_tables()
    pool = await get_pool()
    tags = [str(tag).strip() for tag in (issue_tags or []) if str(tag).strip()]
    row = await pool.fetchrow(
        """
        INSERT INTO briefing_human_reviews (
            briefing_id,
            run_id,
            reviewer,
            decision,
            issue_tags,
            edited_markdown,
            notes
        )
        VALUES ($1, $2, $3, $4, $5::text[], $6, $7)
        RETURNING id
        """,
        briefing_id,
        run_id,
        reviewer or "cli",
        (decision or "").strip().lower(),
        tags,
        edited_markdown,
        notes,
    )
    return row["id"]


async def get_review_queue(
    *,
    limit: int = 20,
    only_unreviewed: bool = False,
) -> list[asyncpg.Record]:
    """List recent briefings with review summary information."""
    await ensure_training_tables()
    await ensure_briefing_items_table()
    item_ids_sql = _briefing_item_ids_sql("b")
    pool = await get_pool()
    where = ["TRUE"]
    if only_unreviewed:
        where.append("COALESCE(rv.review_count, 0) = 0")
    return await pool.fetch(
        f"""
        SELECT
            b.id,
            b.created_at,
            b.status,
            {item_ids_sql} AS item_ids,
            LEFT(b.content_markdown, 220) AS preview,
            COALESCE(rv.review_count, 0)::int AS review_count,
            rv.last_decision,
            rv.last_review_at
        FROM briefings b
        LEFT JOIN LATERAL (
            SELECT
                COUNT(*)::int AS review_count,
                MAX(created_at) AS last_review_at,
                (ARRAY_AGG(decision ORDER BY created_at DESC))[1] AS last_decision
            FROM briefing_human_reviews hr
            WHERE hr.briefing_id = b.id
        ) rv ON TRUE
        WHERE {" AND ".join(where)}
        ORDER BY b.created_at DESC
        LIMIT $1
        """,
        max(1, int(limit)),
    )


async def get_human_reviews(
    *,
    briefing_ids: list[UUID] | None = None,
    run_ids: list[UUID] | None = None,
    limit: int = 0,
) -> list[asyncpg.Record]:
    """Fetch human review rows for export/reporting."""
    await ensure_training_tables()
    pool = await get_pool()
    where = ["TRUE"]
    params: list[object] = []
    if briefing_ids:
        params.append(briefing_ids)
        where.append(f"briefing_id = ANY(${len(params)}::uuid[])")
    if run_ids:
        params.append(run_ids)
        where.append(f"run_id = ANY(${len(params)}::uuid[])")

    sql = (
        "SELECT * FROM briefing_human_reviews "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY created_at DESC"
    )
    if limit > 0:
        params.append(max(1, int(limit)))
        sql += f" LIMIT ${len(params)}"
    return await pool.fetch(sql, *params)


async def upsert_distribution_outcome(
    *,
    briefing_id: UUID,
    channel: str,
    status: str,
    external_message_id: str | None = None,
    external_post_url: str | None = None,
    sent_at: datetime | None = None,
    metrics: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record one distribution attempt (append-only)."""
    await ensure_training_tables()
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO distribution_attempts (
            briefing_id,
            channel,
            status,
            external_message_id,
            external_post_url,
            sent_at,
            metrics,
            metadata
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb)
        """,
        briefing_id,
        (channel or "").strip().lower(),
        (status or "").strip().lower(),
        external_message_id,
        external_post_url,
        sent_at or datetime.now(timezone.utc),
        json.dumps(metrics or {}, ensure_ascii=False, default=str),
        json.dumps(metadata or {}, ensure_ascii=False, default=str),
    )


async def get_distribution_outcomes(
    *,
    briefing_ids: list[UUID] | None = None,
    limit: int = 0,
) -> list[asyncpg.Record]:
    """Fetch latest per-channel distribution outcomes."""
    await ensure_training_tables()
    pool = await get_pool()
    where = ["TRUE"]
    params: list[object] = []
    if briefing_ids:
        params.append(briefing_ids)
        where.append(f"briefing_id = ANY(${len(params)}::uuid[])")

    sql = (
        "SELECT * FROM distribution_outcomes_latest "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY sent_at DESC, created_at DESC"
    )
    if limit > 0:
        params.append(max(1, int(limit)))
        sql += f" LIMIT ${len(params)}"
    return await pool.fetch(sql, *params)


async def get_distribution_attempts(
    *,
    briefing_ids: list[UUID] | None = None,
    limit: int = 0,
) -> list[asyncpg.Record]:
    """Fetch append-only distribution attempt history rows."""
    await ensure_training_tables()
    pool = await get_pool()
    where = ["TRUE"]
    params: list[object] = []
    if briefing_ids:
        params.append(briefing_ids)
        where.append(f"briefing_id = ANY(${len(params)}::uuid[])")

    sql = (
        "SELECT * FROM distribution_attempts "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY sent_at DESC, id DESC"
    )
    if limit > 0:
        params.append(max(1, int(limit)))
        sql += f" LIMIT ${len(params)}"
    return await pool.fetch(sql, *params)


async def get_generation_runs_for_export(
    *,
    limit: int = 0,
    since_days: int = 0,
    include_blocked: bool = False,
) -> list[asyncpg.Record]:
    """Fetch generation runs for dataset export."""
    await ensure_training_tables()
    pool = await get_pool()
    where = ["TRUE"]
    params: list[object] = []
    if not include_blocked:
        where.append("decision = 'PUBLISHED'")
    if since_days > 0:
        params.append(int(since_days))
        where.append(f"created_at > NOW() - make_interval(days => ${len(params)})")

    sql = (
        "SELECT * FROM generation_runs "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY created_at DESC"
    )
    if limit > 0:
        params.append(max(1, int(limit)))
        sql += f" LIMIT ${len(params)}"
    return await pool.fetch(sql, *params)


async def get_generation_rounds_for_runs(run_ids: list[UUID]) -> list[asyncpg.Record]:
    """Fetch per-round artifacts for a set of generation runs."""
    if not run_ids:
        return []
    await ensure_training_tables()
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT *
        FROM generation_rounds
        WHERE run_id = ANY($1::uuid[])
        ORDER BY run_id, round_index ASC
        """,
        run_ids,
    )


async def get_generation_preference_pairs_for_runs(
    run_ids: list[UUID],
) -> list[asyncpg.Record]:
    """Fetch preference pairs linked to generation runs."""
    if not run_ids:
        return []
    await ensure_training_tables()
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT *
        FROM generation_preference_pairs
        WHERE run_id = ANY($1::uuid[])
        ORDER BY created_at ASC
        """,
        run_ids,
    )
