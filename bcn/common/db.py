"""Async PostgreSQL database layer using asyncpg."""

from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import timezone
import hashlib
import json
import logging
from typing import Any, Optional
from uuid import UUID

import asyncpg

from bcn.common.config import Settings

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None
_pool_lock: asyncio.Lock = asyncio.Lock()


async def get_pool(settings: Optional[Settings] = None) -> asyncpg.Pool:
    """Return the shared connection pool, creating it on first call.

    Args:
        settings: Optional settings override. Uses defaults when ``None``.

    Returns:
        The asyncpg connection pool.
    """
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        if _pool is None:
            s = settings or Settings()
            _pool = await asyncpg.create_pool(s.database_url, min_size=2, max_size=10)
    return _pool


async def close_pool() -> None:
    """Close the shared connection pool if it is open."""
    global _pool
    async with _pool_lock:
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


async def get_new_items(
    *,
    limit: int = 250,
    stale_analyzing_minutes: int = 120,
) -> list[asyncpg.Record]:
    """Atomically claim ``NEW`` items for analysis.

    Uses ``FOR UPDATE SKIP LOCKED`` so concurrent analyst workers do not process
    the same items. Stale ``ANALYZING`` rows are automatically reclaimed.
    """
    pool = await get_pool()
    return await pool.fetch(
        """
        WITH candidate AS (
            SELECT id
            FROM news_items
            WHERE status = 'NEW'
               OR (
                    status = 'ANALYZING'
                    AND updated_at < NOW() - make_interval(mins => $2)
               )
            ORDER BY published_at DESC
            FOR UPDATE SKIP LOCKED
            LIMIT $1
        ),
        claimed AS (
            UPDATE news_items AS n
            SET status = 'ANALYZING', updated_at = NOW()
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


async def release_items_from_analyzing(ids: list[UUID]) -> None:
    """Release claimed ``ANALYZING`` items back to ``NEW`` for retry."""
    if not ids:
        return
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE news_items
        SET status = 'NEW', updated_at = NOW()
        WHERE id = ANY($1::uuid[]) AND status = 'ANALYZING'
        """,
        ids,
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
    await pool.execute(
        """
        UPDATE news_items
        SET summary = $1, relevance_score = $2, ai_tags = $3::jsonb,
            full_content = COALESCE($4, full_content),
            image_prompt = $5, url = COALESCE($6, url), status = 'ANALYZED', updated_at = NOW()
        WHERE id = $7
        """,
        summary,
        relevance_score,
        json.dumps(ai_tags),
        full_content,
        image_prompt,
        canonical_url,
        item_id,
    )


async def get_analyzed_items(
    min_score: int = 7,
    hours: int = 24,
    *,
    limit: int = 250,
    stale_writing_minutes: int = 180,
) -> list[asyncpg.Record]:
    """Atomically claim analyzed items for writer selection.

    Uses ``FOR UPDATE SKIP LOCKED`` so concurrent writer workers do not process
    the same candidate set. Stale ``WRITING`` rows are automatically reclaimed.
    """
    pool = await get_pool()
    return await pool.fetch(
        """
        WITH candidate AS (
            SELECT id
            FROM news_items
            WHERE (
                    status = 'ANALYZED'
                    OR (
                        status = 'WRITING'
                        AND updated_at < NOW() - make_interval(mins => $3)
                    )
                  )
              AND relevance_score >= $1
              AND published_at > NOW() - make_interval(hours => $2)
              AND NOT EXISTS (
                  SELECT 1
                  FROM briefings
                  WHERE news_items.id = ANY(briefings.item_ids)
                    AND briefings.status = 'DISTRIBUTED'
              )
            ORDER BY relevance_score DESC, published_at DESC
            FOR UPDATE SKIP LOCKED
            LIMIT $4
        ),
        claimed AS (
            UPDATE news_items AS n
            SET status = 'WRITING', updated_at = NOW()
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
        SET status = 'ANALYZED', updated_at = NOW()
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
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT *
        FROM news_items
        WHERE status = ANY($1::text[])
          AND relevance_score >= $2
          AND summary IS NOT NULL
          AND published_at > NOW() - make_interval(days => $3)
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
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT id, source_type, url, title, summary, ai_tags, relevance_score, published_at, raw_data
        FROM news_items
        WHERE status = 'PUBLISHED'
          AND published_at > NOW() - make_interval(hours => $1)
        ORDER BY published_at DESC
        LIMIT $2
        """,
        hours,
        limit,
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
    pool = await get_pool()

    where = ["status = 'DISTRIBUTED'"]
    params: list[object] = []

    if since_days > 0:
        params.append(int(since_days))
        where.append(f"created_at > NOW() - make_interval(days => ${len(params)})")

    sql = (
        "SELECT id, created_at, distributed_at, content_markdown, item_ids "
        "FROM briefings "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY created_at DESC"
    )
    if limit > 0:
        params.append(int(limit))
        sql += f" LIMIT ${len(params)}"

    return await pool.fetch(sql, *params)


async def get_latest_any_briefing() -> Optional[asyncpg.Record]:
    """Return the latest briefing regardless of status."""
    pool = await get_pool()
    return await pool.fetchrow(
        "SELECT * FROM briefings ORDER BY created_at DESC LIMIT 1"
    )


async def get_latest_briefing() -> Optional[asyncpg.Record]:
    """Return the most recent ``DRAFT`` briefing, or ``None``."""
    pool = await get_pool()
    return await pool.fetchrow(
        "SELECT * FROM briefings WHERE status = 'DRAFT' ORDER BY created_at DESC LIMIT 1"
    )


async def claim_latest_draft_briefing() -> Optional[asyncpg.Record]:
    """Atomically claim the latest draft for distribution.

    Transitions one ``DRAFT`` row to ``DISTRIBUTING`` using ``SKIP LOCKED`` so
    concurrent distributor runs cannot claim the same briefing. Also reclaims
    stale ``DISTRIBUTING`` rows older than 30 minutes (e.g., crashed workers).
    """
    pool = await get_pool()
    return await pool.fetchrow(
        """
        WITH candidate AS (
            SELECT id
            FROM briefings
            WHERE status = 'DRAFT'
               OR (
                    status = 'DISTRIBUTING'
                    AND updated_at < NOW() - INTERVAL '30 minutes'
               )
            ORDER BY created_at DESC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        UPDATE briefings AS b
        SET status = 'DISTRIBUTING', updated_at = NOW()
        FROM candidate
        WHERE b.id = candidate.id
        RETURNING b.*
        """
    )


async def claim_draft_briefing_by_id(briefing_id: UUID) -> Optional[asyncpg.Record]:
    """Atomically claim a specific draft briefing for distribution."""
    pool = await get_pool()
    return await pool.fetchrow(
        """
        UPDATE briefings
        SET status = 'DISTRIBUTING', updated_at = NOW()
        WHERE id = $1
          AND (
            status = 'DRAFT'
            OR (
                status = 'DISTRIBUTING'
                AND updated_at < NOW() - INTERVAL '30 minutes'
            )
          )
        RETURNING *
        """,
        briefing_id,
    )


async def release_briefing_for_retry(briefing_id: UUID) -> None:
    """Return a claimed briefing back to ``DRAFT`` for retry."""
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE briefings
        SET status = 'DRAFT', updated_at = NOW()
        WHERE id = $1 AND status = 'DISTRIBUTING'
        """,
        briefing_id,
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
        SET status = 'DISTRIBUTED', distributed_at = NOW(), distribution_channels = $1::jsonb, updated_at = NOW()
        WHERE id = $2
        """,
        json.dumps(channels),
        briefing_id,
    )


# ---------------------------------------------------------------------------
# Historical Channel Posts
# ---------------------------------------------------------------------------


async def ensure_history_tables() -> None:
    """Create history tables used for imported previously published posts."""
    pool = await get_pool()
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS published_history_posts (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            channel VARCHAR(32) NOT NULL,
            author TEXT,
            posted_at TIMESTAMP WITH TIME ZONE NOT NULL,
            content_markdown TEXT NOT NULL,
            content_hash VARCHAR(64) NOT NULL UNIQUE,
            urls JSONB NOT NULL DEFAULT '[]'::jsonb,
            item_ids UUID[] NOT NULL DEFAULT '{}'::uuid[],
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
        """)
    await pool.execute(
        "CREATE INDEX IF NOT EXISTS idx_published_history_posts_posted_at "
        "ON published_history_posts (posted_at DESC)"
    )
    await pool.execute(
        "CREATE INDEX IF NOT EXISTS idx_published_history_posts_channel_posted_at "
        "ON published_history_posts (channel, posted_at DESC)"
    )


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
            status
        )
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, 'PUBLISHED')
        ON CONFLICT (source_type, source_id) DO UPDATE
        SET
            status = 'PUBLISHED',
            published_at = LEAST(news_items.published_at, EXCLUDED.published_at),
            updated_at = NOW(),
            title = COALESCE(news_items.title, EXCLUDED.title),
            raw_data = COALESCE(news_items.raw_data, '{}'::jsonb) || EXCLUDED.raw_data
        RETURNING id
        """,
        source_type,
        source_id,
        url,
        title,
        posted_at,
        json.dumps(payload, ensure_ascii=False, default=str),
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
    """Create newsletter subscriber tables if they do not already exist."""
    pool = await get_pool()
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS newsletter_subscribers (
            id BIGSERIAL PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
        """)
    await pool.execute(
        "CREATE INDEX IF NOT EXISTS idx_newsletter_subscribers_active_email "
        "ON newsletter_subscribers (is_active, email)"
    )


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


async def ensure_simulation_tables() -> None:
    """Create simulation persistence tables if they do not already exist."""
    pool = await get_pool()
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS simulation_runs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            generated_at TIMESTAMP WITH TIME ZONE,
            source VARCHAR(64) NOT NULL DEFAULT 'cli',
            report_path TEXT,
            params JSONB NOT NULL DEFAULT '{}'::jsonb,
            summary JSONB NOT NULL DEFAULT '{}'::jsonb,
            count INTEGER NOT NULL DEFAULT 0,
            notes TEXT,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
        """)
    await pool.execute(
        "CREATE INDEX IF NOT EXISTS idx_simulation_runs_created_at ON simulation_runs (created_at DESC)"
    )
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS simulation_results (
            id BIGSERIAL PRIMARY KEY,
            run_id UUID NOT NULL REFERENCES simulation_runs(id) ON DELETE CASCADE,
            briefing_id TEXT,
            briefing_created_at TIMESTAMP WITH TIME ZONE,
            actual_score INTEGER NOT NULL,
            simulated_score INTEGER NOT NULL,
            delta INTEGER NOT NULL,
            result JSONB NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            UNIQUE (run_id, briefing_id)
        )
        """)
    await pool.execute(
        "CREATE INDEX IF NOT EXISTS idx_simulation_results_run_id ON simulation_results (run_id)"
    )
    await pool.execute(
        "CREATE INDEX IF NOT EXISTS idx_simulation_results_briefing_id ON simulation_results (briefing_id)"
    )


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

    params_raw = run["params"]
    if isinstance(params_raw, dict):
        params = params_raw
    elif isinstance(params_raw, str):
        try:
            parsed_params = json.loads(params_raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed_params = {}
        params = parsed_params if isinstance(parsed_params, dict) else {}
    else:
        params = {}

    summary_raw = run["summary"]
    if isinstance(summary_raw, dict):
        summary = summary_raw
    elif isinstance(summary_raw, str):
        try:
            parsed_summary = json.loads(summary_raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed_summary = {}
        summary = parsed_summary if isinstance(parsed_summary, dict) else {}
    else:
        summary = {}
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
# Fine-Tuning Data: Traces, Preferences, Reviews, Outcomes
# ---------------------------------------------------------------------------


async def ensure_training_tables() -> None:
    """Create trace/review/outcome tables used for fine-tuning datasets."""
    pool = await get_pool()
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS generation_runs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            trigger_source VARCHAR(64) NOT NULL DEFAULT 'writer',
            mode VARCHAR(32) NOT NULL DEFAULT 'standard',
            decision VARCHAR(16) NOT NULL DEFAULT 'PENDING',
            decision_reason TEXT,
            rewrite_count INTEGER NOT NULL DEFAULT 0,
            briefing_id UUID REFERENCES briefings(id) ON DELETE SET NULL,
            selected_item_ids UUID[] NOT NULL DEFAULT '{}'::uuid[],
            selected_items JSONB NOT NULL DEFAULT '[]'::jsonb,
            llm_model TEXT,
            llm_model_version TEXT,
            prompts JSONB NOT NULL DEFAULT '{}'::jsonb,
            config_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
            git_sha VARCHAR(128),
            initial_draft TEXT,
            final_draft TEXT,
            final_gate JSONB NOT NULL DEFAULT '{}'::jsonb,
            final_critique JSONB NOT NULL DEFAULT '{}'::jsonb,
            final_verifier JSONB NOT NULL DEFAULT '{}'::jsonb
        )
        """)
    await pool.execute(
        "CREATE INDEX IF NOT EXISTS idx_generation_runs_created_at ON generation_runs (created_at DESC)"
    )
    await pool.execute(
        "CREATE INDEX IF NOT EXISTS idx_generation_runs_briefing_id ON generation_runs (briefing_id)"
    )

    await pool.execute("""
        CREATE TABLE IF NOT EXISTS generation_rounds (
            id BIGSERIAL PRIMARY KEY,
            run_id UUID NOT NULL REFERENCES generation_runs(id) ON DELETE CASCADE,
            round_index INTEGER NOT NULL,
            phase VARCHAR(32) NOT NULL DEFAULT 'initial',
            draft_input TEXT,
            gate_result JSONB NOT NULL DEFAULT '{}'::jsonb,
            critique_result JSONB NOT NULL DEFAULT '{}'::jsonb,
            verifier_result JSONB NOT NULL DEFAULT '{}'::jsonb,
            feedback JSONB NOT NULL DEFAULT '[]'::jsonb,
            rewrite_output TEXT,
            passed BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            UNIQUE (run_id, round_index)
        )
        """)
    await pool.execute(
        "CREATE INDEX IF NOT EXISTS idx_generation_rounds_run_id ON generation_rounds (run_id)"
    )

    await pool.execute("""
        CREATE TABLE IF NOT EXISTS generation_preference_pairs (
            id BIGSERIAL PRIMARY KEY,
            run_id UUID NOT NULL REFERENCES generation_runs(id) ON DELETE CASCADE,
            round_index INTEGER NOT NULL DEFAULT 0,
            source VARCHAR(64) NOT NULL DEFAULT 'auto_writer_loop',
            chosen_text TEXT NOT NULL,
            rejected_text TEXT NOT NULL,
            rationale TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
        """)
    await pool.execute(
        "CREATE INDEX IF NOT EXISTS idx_generation_preference_pairs_run_id "
        "ON generation_preference_pairs (run_id)"
    )

    await pool.execute("""
        CREATE TABLE IF NOT EXISTS briefing_human_reviews (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            briefing_id UUID NOT NULL REFERENCES briefings(id) ON DELETE CASCADE,
            run_id UUID REFERENCES generation_runs(id) ON DELETE SET NULL,
            reviewer VARCHAR(128) NOT NULL DEFAULT 'cli',
            decision VARCHAR(16) NOT NULL,
            issue_tags TEXT[] NOT NULL DEFAULT '{}'::text[],
            edited_markdown TEXT,
            notes TEXT,
            CHECK (decision IN ('accept', 'reject', 'edit', 'needs_work'))
        )
        """)
    await pool.execute(
        "CREATE INDEX IF NOT EXISTS idx_briefing_human_reviews_briefing_id "
        "ON briefing_human_reviews (briefing_id, created_at DESC)"
    )

    await pool.execute("""
        CREATE TABLE IF NOT EXISTS briefing_distribution_outcomes (
            id BIGSERIAL PRIMARY KEY,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            sent_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            briefing_id UUID NOT NULL REFERENCES briefings(id) ON DELETE CASCADE,
            channel VARCHAR(32) NOT NULL,
            status VARCHAR(32) NOT NULL,
            external_message_id TEXT,
            external_post_url TEXT,
            metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            UNIQUE (briefing_id, channel)
        )
        """)
    await pool.execute(
        "CREATE INDEX IF NOT EXISTS idx_distribution_outcomes_briefing_id "
        "ON briefing_distribution_outcomes (briefing_id)"
    )


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
    pool = await get_pool()
    return await pool.fetchrow(
        "SELECT * FROM briefings WHERE id = $1 LIMIT 1",
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
            b.item_ids,
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
    """Upsert per-channel distribution outcome and engagement metrics."""
    await ensure_training_tables()
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO briefing_distribution_outcomes (
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
        ON CONFLICT (briefing_id, channel) DO UPDATE
        SET
            status = EXCLUDED.status,
            external_message_id = COALESCE(EXCLUDED.external_message_id, briefing_distribution_outcomes.external_message_id),
            external_post_url = COALESCE(EXCLUDED.external_post_url, briefing_distribution_outcomes.external_post_url),
            sent_at = EXCLUDED.sent_at,
            metrics = COALESCE(NULLIF(EXCLUDED.metrics, '{}'::jsonb), briefing_distribution_outcomes.metrics),
            metadata = briefing_distribution_outcomes.metadata || EXCLUDED.metadata,
            updated_at = NOW()
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
    """Fetch stored distribution outcome rows."""
    await ensure_training_tables()
    pool = await get_pool()
    where = ["TRUE"]
    params: list[object] = []
    if briefing_ids:
        params.append(briefing_ids)
        where.append(f"briefing_id = ANY(${len(params)}::uuid[])")

    sql = (
        "SELECT * FROM briefing_distribution_outcomes "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY sent_at DESC, created_at DESC"
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
