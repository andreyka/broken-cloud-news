"""Persistence gateway for the durable workflow job queue."""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any
from uuid import UUID

import asyncpg

from bcn.persistence.runtime import ensure_schema_ready
from bcn.persistence.runtime import get_pool

_JOB_ERROR_MAX_LEN = 4000


def _json_payload(value: object) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)


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


def _normalize_error(error: str | None, *, fallback: str) -> str:
    value = str(error or "").strip()
    if not value:
        value = fallback
    return value[:_JOB_ERROR_MAX_LEN]


def _normalize_lane(lane: str | None) -> str:
    return str(lane or "").strip().lower()


async def ensure_workflow_job_tables() -> None:
    """Ensure schema migrations already created workflow job tables."""
    await ensure_schema_ready()


async def create_workflow_job(
    *,
    lane: str,
    priority: int,
    job_type: str,
    source: str = "scheduler",
    workflow_id: str | None = None,
    dedupe_key: str | None = None,
    max_attempts: int = 3,
    lease_duration_seconds: int = 900,
    payload: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    notes: str | None = None,
    available_at: datetime | None = None,
    deadline_at: datetime | None = None,
) -> UUID:
    """Insert one queued workflow job."""
    await ensure_workflow_job_tables()
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO workflow_jobs (
            lane,
            priority,
            job_type,
            source,
            workflow_id,
            dedupe_key,
            max_attempts,
            lease_duration_seconds,
            payload,
            state,
            notes,
            available_at,
            deadline_at
        )
        VALUES (
            $1,
            $2,
            $3,
            $4,
            $5,
            $6,
            $7,
            $8,
            $9::jsonb,
            $10::jsonb,
            $11,
            COALESCE($12, NOW()),
            $13
        )
        ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL AND status IN ('queued', 'leased')
        DO UPDATE
        SET
            updated_at = NOW(),
            notes = COALESCE(EXCLUDED.notes, workflow_jobs.notes)
        RETURNING id
        """,
        _normalize_lane(lane),
        int(priority),
        str(job_type or "").strip(),
        str(source or "scheduler").strip(),
        str(workflow_id).strip() if workflow_id else None,
        str(dedupe_key).strip() if dedupe_key else None,
        max(1, int(max_attempts)),
        max(30, int(lease_duration_seconds)),
        _json_payload(payload or {}),
        _json_payload(state or {}),
        notes,
        available_at,
        deadline_at,
    )
    return row["id"]


async def reclaim_expired_workflow_job_leases() -> list[UUID]:
    """Requeue jobs whose lease expired before a worker renewed it."""
    await ensure_workflow_job_tables()
    pool = await get_pool()
    rows = await pool.fetch(
        """
        UPDATE workflow_jobs
        SET
            status = 'queued',
            lease_owner = NULL,
            lease_expires_at = NULL,
            heartbeat_at = NULL,
            available_at = NOW(),
            updated_at = NOW(),
            error_message = COALESCE(error_message, 'lease_expired')
        WHERE status = 'leased'
          AND lease_expires_at IS NOT NULL
          AND lease_expires_at < NOW()
        RETURNING id
        """
    )
    job_ids = [row["id"] for row in rows]
    if job_ids:
        await pool.execute(
            """
            UPDATE workflow_job_attempts
            SET
                status = 'expired',
                finished_at = NOW(),
                error_message = COALESCE(error_message, 'lease_expired')
            WHERE job_id = ANY($1::uuid[])
              AND status = 'running'
            """,
            job_ids,
        )
    return job_ids


async def cancel_expired_workflow_jobs() -> list[UUID]:
    """Cancel queued or leased jobs whose deadline passed."""
    await ensure_workflow_job_tables()
    pool = await get_pool()
    rows = await pool.fetch(
        """
        UPDATE workflow_jobs
        SET
            status = 'canceled',
            finished_at = NOW(),
            updated_at = NOW(),
            lease_owner = NULL,
            lease_expires_at = NULL,
            heartbeat_at = NULL,
            error_message = COALESCE(error_message, 'deadline_exceeded')
        WHERE status IN ('queued', 'leased')
          AND deadline_at IS NOT NULL
          AND deadline_at <= NOW()
        RETURNING id
        """
    )
    job_ids = [row["id"] for row in rows]
    if job_ids:
        await pool.execute(
            """
            UPDATE workflow_job_attempts
            SET
                status = 'canceled',
                finished_at = NOW(),
                error_message = COALESCE(error_message, 'deadline_exceeded')
            WHERE job_id = ANY($1::uuid[])
              AND status = 'running'
            """,
            job_ids,
        )
    return job_ids


async def claim_next_workflow_job(
    *,
    lanes: list[str],
    worker_id: str,
) -> dict[str, Any] | None:
    """Lease the next available workflow job for one worker."""
    await ensure_workflow_job_tables()
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                WITH candidate AS (
                    SELECT id
                    FROM workflow_jobs
                    WHERE status = 'queued'
                      AND lane = ANY($1::varchar[])
                      AND available_at <= NOW()
                      AND attempt_count < max_attempts
                      AND (deadline_at IS NULL OR deadline_at > NOW())
                      AND NOT EXISTS (
                          SELECT 1
                          FROM workflow_lane_controls controls
                          WHERE controls.lane = workflow_jobs.lane
                            AND controls.paused = TRUE
                      )
                    ORDER BY priority DESC, available_at ASC, created_at ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE workflow_jobs AS j
                SET
                    status = 'leased',
                    attempt_count = attempt_count + 1,
                    started_at = COALESCE(started_at, NOW()),
                    lease_owner = $2,
                    lease_expires_at = NOW() + make_interval(secs => j.lease_duration_seconds),
                    heartbeat_at = NOW(),
                    updated_at = NOW()
                FROM candidate
                WHERE j.id = candidate.id
                RETURNING
                    j.id,
                    j.created_at,
                    j.updated_at,
                    j.available_at,
                    j.started_at,
                    j.finished_at,
                    j.deadline_at,
                    j.lane,
                    j.priority,
                    j.job_type,
                    j.source,
                    j.workflow_id,
                    j.dedupe_key,
                    j.status,
                    j.attempt_count,
                    j.max_attempts,
                    j.lease_duration_seconds,
                    j.lease_owner,
                    j.lease_expires_at,
                    j.heartbeat_at,
                    j.payload,
                    j.state,
                    j.result,
                    j.error_message,
                    j.notes
                """,
                [_normalize_lane(lane) for lane in lanes],
                str(worker_id or "").strip(),
            )
            if not row:
                return None
            attempt = await conn.fetchrow(
                """
                INSERT INTO workflow_job_attempts (
                    job_id,
                    attempt_number,
                    worker_id,
                    status,
                    state_before
                )
                VALUES ($1, $2, $3, 'running', $4::jsonb)
                RETURNING id
                """,
                row["id"],
                int(row["attempt_count"]),
                str(worker_id or "").strip(),
                _json_payload(_coerce_json_dict(row["state"])),
            )
    payload = dict(row)
    payload["payload"] = _coerce_json_dict(payload.get("payload"))
    payload["state"] = _coerce_json_dict(payload.get("state"))
    payload["result"] = _coerce_json_dict(payload.get("result"))
    payload["attempt_id"] = int(attempt["id"]) if attempt else None
    return payload


async def renew_workflow_job_lease(
    job_id: UUID,
    *,
    worker_id: str,
    state: dict[str, Any] | None = None,
) -> bool:
    """Renew the active lease for one claimed workflow job."""
    await ensure_workflow_job_tables()
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        UPDATE workflow_jobs
        SET
            lease_expires_at = NOW() + make_interval(secs => lease_duration_seconds),
            heartbeat_at = NOW(),
            state = COALESCE($3::jsonb, state),
            updated_at = NOW()
        WHERE id = $1
          AND status = 'leased'
          AND lease_owner = $2
        RETURNING id
        """,
        job_id,
        str(worker_id or "").strip(),
        _json_payload(state) if state is not None else None,
    )
    return row is not None


async def update_workflow_job_progress(
    job_id: UUID,
    *,
    worker_id: str,
    state: dict[str, Any],
    attempt_id: int | None = None,
    artifact_key: str | None = None,
    artifact_type: str | None = None,
    artifact_payload: dict[str, Any] | None = None,
) -> None:
    """Persist one in-flight job state snapshot and optional artifact."""
    await ensure_workflow_job_tables()
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE workflow_jobs
        SET
            state = $3::jsonb,
            heartbeat_at = NOW(),
            lease_expires_at = NOW() + make_interval(secs => lease_duration_seconds),
            updated_at = NOW()
        WHERE id = $1
          AND status = 'leased'
          AND lease_owner = $2
        """,
        job_id,
        str(worker_id or "").strip(),
        _json_payload(state),
    )
    if artifact_key and artifact_type:
        await pool.execute(
            """
            INSERT INTO workflow_job_artifacts (
                job_id,
                attempt_id,
                artifact_key,
                artifact_type,
                payload
            )
            VALUES ($1, $2, $3, $4, $5::jsonb)
            ON CONFLICT (job_id, artifact_key)
            DO UPDATE
            SET
                attempt_id = EXCLUDED.attempt_id,
                artifact_type = EXCLUDED.artifact_type,
                payload = EXCLUDED.payload,
                updated_at = NOW()
            """,
            job_id,
            attempt_id,
            str(artifact_key),
            str(artifact_type),
            _json_payload(artifact_payload or state),
        )


async def complete_workflow_job(
    job_id: UUID,
    *,
    worker_id: str,
    result: dict[str, Any],
    attempt_id: int | None = None,
) -> None:
    """Mark one leased workflow job as completed."""
    await ensure_workflow_job_tables()
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE workflow_jobs
        SET
            status = 'completed',
            result = $3::jsonb,
            state = COALESCE(NULLIF(state, '{}'::jsonb), $3::jsonb),
            finished_at = NOW(),
            updated_at = NOW(),
            lease_owner = NULL,
            lease_expires_at = NULL,
            heartbeat_at = NOW(),
            error_message = NULL
        WHERE id = $1
          AND status = 'leased'
          AND lease_owner = $2
        """,
        job_id,
        str(worker_id or "").strip(),
        _json_payload(result),
    )
    if attempt_id is not None:
        await pool.execute(
            """
            UPDATE workflow_job_attempts
            SET
                status = 'completed',
                finished_at = NOW(),
                state_after = $2::jsonb,
                result = $3::jsonb
            WHERE id = $1
            """,
            int(attempt_id),
            _json_payload(result),
            _json_payload(result),
        )


async def requeue_workflow_job(
    job_id: UUID,
    *,
    worker_id: str,
    error_message: str,
    delay_seconds: int,
    state: dict[str, Any] | None = None,
    attempt_id: int | None = None,
) -> None:
    """Return one leased workflow job to the queue after a retryable failure."""
    await ensure_workflow_job_tables()
    pool = await get_pool()
    message = _normalize_error(error_message, fallback="workflow_job_retry")
    await pool.execute(
        """
        UPDATE workflow_jobs
        SET
            status = 'queued',
            available_at = NOW() + make_interval(secs => $3),
            state = COALESCE($4::jsonb, state),
            updated_at = NOW(),
            lease_owner = NULL,
            lease_expires_at = NULL,
            heartbeat_at = NOW(),
            error_message = $5
        WHERE id = $1
          AND status = 'leased'
          AND lease_owner = $2
        """,
        job_id,
        str(worker_id or "").strip(),
        max(1, int(delay_seconds)),
        _json_payload(state) if state is not None else None,
        message,
    )
    if attempt_id is not None:
        await pool.execute(
            """
            UPDATE workflow_job_attempts
            SET
                status = 'failed',
                finished_at = NOW(),
                error_message = $2,
                state_after = COALESCE($3::jsonb, state_after)
            WHERE id = $1
            """,
            int(attempt_id),
            message,
            _json_payload(state) if state is not None else None,
        )


async def fail_workflow_job(
    job_id: UUID,
    *,
    worker_id: str,
    error_message: str,
    state: dict[str, Any] | None = None,
    attempt_id: int | None = None,
    canceled: bool = False,
) -> None:
    """Mark one leased workflow job as failed or canceled."""
    await ensure_workflow_job_tables()
    pool = await get_pool()
    message = _normalize_error(error_message, fallback="workflow_job_failed")
    status = "canceled" if canceled else "failed"
    await pool.execute(
        f"""
        UPDATE workflow_jobs
        SET
            status = '{status}',
            state = COALESCE($3::jsonb, state),
            finished_at = NOW(),
            updated_at = NOW(),
            lease_owner = NULL,
            lease_expires_at = NULL,
            heartbeat_at = NOW(),
            error_message = $4
        WHERE id = $1
          AND status = 'leased'
          AND lease_owner = $2
        """,
        job_id,
        str(worker_id or "").strip(),
        _json_payload(state) if state is not None else None,
        message,
    )
    if attempt_id is not None:
        await pool.execute(
            f"""
            UPDATE workflow_job_attempts
            SET
                status = '{status}',
                finished_at = NOW(),
                error_message = $2,
                state_after = COALESCE($3::jsonb, state_after)
            WHERE id = $1
            """,
            int(attempt_id),
            message,
            _json_payload(state) if state is not None else None,
        )


async def get_workflow_job(job_id: UUID) -> dict[str, Any] | None:
    """Return one workflow job payload by id."""
    await ensure_workflow_job_tables()
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        SELECT *
        FROM workflow_jobs
        WHERE id = $1
        """,
        job_id,
    )
    if not row:
        return None
    payload = dict(row)
    payload["payload"] = _coerce_json_dict(payload.get("payload"))
    payload["state"] = _coerce_json_dict(payload.get("state"))
    payload["result"] = _coerce_json_dict(payload.get("result"))
    return payload


async def set_workflow_lane_control(
    lane: str,
    *,
    paused: bool,
    updated_by: str = "cli",
    reason: str | None = None,
) -> dict[str, Any]:
    """Upsert one lane control row used to pause or resume job claiming."""
    await ensure_workflow_job_tables()
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO workflow_lane_controls (
            lane,
            paused,
            reason,
            updated_by,
            paused_at,
            updated_at
        )
        VALUES (
            $1,
            $2,
            NULLIF($3, ''),
            NULLIF($4, ''),
            CASE WHEN $2 THEN NOW() ELSE NULL END,
            NOW()
        )
        ON CONFLICT (lane)
        DO UPDATE
        SET
            paused = EXCLUDED.paused,
            reason = EXCLUDED.reason,
            updated_by = EXCLUDED.updated_by,
            paused_at = CASE WHEN EXCLUDED.paused THEN NOW() ELSE NULL END,
            updated_at = NOW()
        RETURNING lane, paused, reason, updated_by, paused_at, updated_at
        """,
        _normalize_lane(lane),
        bool(paused),
        str(reason or "").strip(),
        str(updated_by or "").strip(),
    )
    return dict(row) if row else {}


async def list_workflow_lane_controls() -> list[asyncpg.Record]:
    """Return persisted lane pause state for operator controls."""
    await ensure_workflow_job_tables()
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT
            lane,
            paused,
            reason,
            updated_by,
            paused_at,
            updated_at
        FROM workflow_lane_controls
        ORDER BY lane ASC
        """
    )


async def list_recent_workflow_jobs(
    *,
    lane: str | None = None,
    limit: int = 20,
) -> list[asyncpg.Record]:
    """Return recent workflow jobs for operator inspection."""
    await ensure_workflow_job_tables()
    pool = await get_pool()
    row_limit = max(1, int(limit))
    if lane:
        return await pool.fetch(
            """
            SELECT
                id,
                created_at,
                available_at,
                started_at,
                finished_at,
                deadline_at,
                lane,
                priority,
                job_type,
                source,
                workflow_id,
                status,
                attempt_count,
                max_attempts,
                error_message,
                notes
            FROM workflow_jobs
            WHERE lane = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            str(lane or "").strip().lower(),
            row_limit,
        )
    return await pool.fetch(
        """
        SELECT
            id,
            created_at,
            available_at,
            started_at,
            finished_at,
            deadline_at,
            lane,
            priority,
            job_type,
            source,
            workflow_id,
            status,
            attempt_count,
            max_attempts,
            error_message,
            notes
        FROM workflow_jobs
        ORDER BY created_at DESC
        LIMIT $1
        """,
        row_limit,
    )


__all__ = [
    "cancel_expired_workflow_jobs",
    "claim_next_workflow_job",
    "complete_workflow_job",
    "create_workflow_job",
    "ensure_workflow_job_tables",
    "fail_workflow_job",
    "get_workflow_job",
    "list_workflow_lane_controls",
    "list_recent_workflow_jobs",
    "reclaim_expired_workflow_job_leases",
    "requeue_workflow_job",
    "renew_workflow_job_lease",
    "set_workflow_lane_control",
    "update_workflow_job_progress",
]
