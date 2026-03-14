"""Persistence gateway for offline optimization runs."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import asyncpg

from bcn.persistence.runtime import ensure_schema_ready
from bcn.persistence.runtime import get_pool


async def ensure_optimization_tables() -> None:
    """Ensure optimization tables exist via schema migrations."""
    await ensure_schema_ready()


async def create_optimization_run(
    *,
    source: str,
    git_sha: str | None,
    benchmark_pack_path: str | None,
    replay_limit: int,
    replay_since_days: int,
    notes: str | None = None,
) -> UUID:
    await ensure_optimization_tables()
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO optimization_runs (
            source,
            git_sha,
            benchmark_pack_path,
            replay_limit,
            replay_since_days,
            notes
        )
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id
        """,
        source,
        git_sha,
        benchmark_pack_path,
        int(replay_limit),
        int(replay_since_days),
        notes,
    )
    return row["id"]


async def complete_optimization_run(run_id: UUID, *, notes: str | None = None) -> None:
    await ensure_optimization_tables()
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE optimization_runs
        SET status = 'COMPLETED',
            notes = COALESCE($2, notes),
            updated_at = NOW()
        WHERE id = $1
        """,
        run_id,
        notes,
    )


async def partial_optimization_run(run_id: UUID, *, notes: str | None = None) -> None:
    await ensure_optimization_tables()
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE optimization_runs
        SET status = 'PARTIAL',
            notes = COALESCE($2, notes),
            updated_at = NOW()
        WHERE id = $1
        """,
        run_id,
        notes,
    )


async def fail_optimization_run(run_id: UUID, *, notes: str | None = None) -> None:
    await ensure_optimization_tables()
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE optimization_runs
        SET status = 'FAILED',
            notes = COALESCE($2, notes),
            updated_at = NOW()
        WHERE id = $1
        """,
        run_id,
        notes,
    )


async def insert_optimization_candidate(
    *,
    optimization_run_id: UUID,
    variant_id: str,
    base_variant: str,
    variant_payload: dict[str, Any],
) -> UUID:
    await ensure_optimization_tables()
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO optimization_candidates (
            optimization_run_id,
            variant_id,
            base_variant,
            variant_payload
        )
        VALUES ($1, $2, $3, $4::jsonb)
        RETURNING id
        """,
        optimization_run_id,
        variant_id,
        base_variant,
        json.dumps(variant_payload, ensure_ascii=False, default=str),
    )
    return row["id"]


async def complete_optimization_candidate(
    candidate_id: UUID,
    *,
    hard_reject: bool,
    recommendation: str,
    composite_score: float,
    summary: dict[str, Any],
) -> None:
    await ensure_optimization_tables()
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE optimization_candidates
        SET status = 'COMPLETED',
            hard_reject = $2,
            recommendation = $3,
            composite_score = $4,
            summary = $5::jsonb,
            updated_at = NOW()
        WHERE id = $1
        """,
        candidate_id,
        bool(hard_reject),
        recommendation,
        float(composite_score),
        json.dumps(summary, ensure_ascii=False, default=str),
    )


async def partial_optimization_candidate(
    candidate_id: UUID,
    *,
    summary: dict[str, Any] | None = None,
) -> None:
    await ensure_optimization_tables()
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE optimization_candidates
        SET status = 'PARTIAL',
            summary = $2::jsonb,
            updated_at = NOW()
        WHERE id = $1
        """,
        candidate_id,
        json.dumps(summary or {}, ensure_ascii=False, default=str),
    )


async def fail_optimization_candidate(
    candidate_id: UUID,
    *,
    summary: dict[str, Any] | None = None,
) -> None:
    await ensure_optimization_tables()
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE optimization_candidates
        SET status = 'FAILED',
            summary = $2::jsonb,
            updated_at = NOW()
        WHERE id = $1
        """,
        candidate_id,
        json.dumps(summary or {}, ensure_ascii=False, default=str),
    )


async def insert_optimization_candidate_lane_result(
    *,
    optimization_candidate_id: UUID,
    lane: str,
    report: dict[str, Any],
    summary: dict[str, Any],
    status: str = "COMPLETED",
    error_text: str | None = None,
    hard_reject: bool,
    score: float | None,
) -> None:
    await ensure_optimization_tables()
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO optimization_candidate_lane_results (
            optimization_candidate_id,
            lane,
            report,
            summary,
            status,
            error_text,
            hard_reject,
            score
        )
        VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6, $7, $8)
        """,
        optimization_candidate_id,
        lane,
        json.dumps(report, ensure_ascii=False, default=str),
        json.dumps(summary, ensure_ascii=False, default=str),
        status,
        error_text,
        bool(hard_reject),
        float(score) if score is not None else None,
    )


async def list_recent_optimization_runs(limit: int = 20) -> list[asyncpg.Record]:
    await ensure_optimization_tables()
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT
            r.id,
            r.created_at,
            r.status,
            r.source,
            r.git_sha,
            c.variant_id,
            c.recommendation,
            c.composite_score,
            c.hard_reject
        FROM optimization_runs r
        LEFT JOIN optimization_candidates c
          ON c.optimization_run_id = r.id
        ORDER BY r.created_at DESC, c.created_at DESC
        LIMIT $1
        """,
        max(1, int(limit)),
    )


async def get_optimization_candidates_for_export(
    *,
    limit: int = 0,
    since_days: int = 0,
) -> list[asyncpg.Record]:
    """Fetch completed optimization candidates and parent run metadata."""
    await ensure_optimization_tables()
    pool = await get_pool()
    sql = """
        SELECT
            c.id,
            c.optimization_run_id,
            c.variant_id,
            c.base_variant,
            c.variant_payload,
            c.hard_reject,
            c.recommendation,
            c.composite_score,
            c.summary,
            c.created_at,
            r.source,
            r.git_sha,
            r.benchmark_pack_path,
            r.replay_limit,
            r.replay_since_days
        FROM optimization_candidates c
        JOIN optimization_runs r
          ON r.id = c.optimization_run_id
        WHERE c.status = 'COMPLETED'
          AND r.status = 'COMPLETED'
    """
    params: list[object] = []
    if since_days > 0:
        params.append(int(since_days))
        sql += f" AND c.created_at >= NOW() - make_interval(days => ${len(params)})"
    sql += " ORDER BY c.created_at DESC"
    if limit > 0:
        params.append(int(limit))
        sql += f" LIMIT ${len(params)}"
    return await pool.fetch(sql, *params)


async def get_optimization_candidate_lane_results(
    candidate_ids: list[UUID],
) -> list[asyncpg.Record]:
    """Fetch lane result payloads for optimization candidates."""
    if not candidate_ids:
        return []
    await ensure_optimization_tables()
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT *
        FROM optimization_candidate_lane_results
        WHERE optimization_candidate_id = ANY($1::uuid[])
        ORDER BY optimization_candidate_id, lane, created_at ASC
        """,
        candidate_ids,
    )
