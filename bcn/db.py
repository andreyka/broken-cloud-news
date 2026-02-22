"""Async PostgreSQL database layer using asyncpg."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional
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
    await pool.execute(
        """
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
        """
    )
    await pool.execute(
        "CREATE INDEX IF NOT EXISTS idx_simulation_runs_created_at ON simulation_runs (created_at DESC)"
    )
    await pool.execute(
        """
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
        """
    )
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
        payloads: list[tuple[UUID, str | None, datetime | None, int, int, int, str]] = []
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
    return await pool.fetchrow(
        """
        SELECT *
        FROM simulation_runs
        ORDER BY created_at DESC
        LIMIT 1
        """
    )


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
        "generated_at": generated_at.isoformat() if isinstance(generated_at, datetime) else None,
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
