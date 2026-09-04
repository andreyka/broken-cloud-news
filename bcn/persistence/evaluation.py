"""Persistence gateway for simulation and evaluation run storage."""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any
from typing import Optional
from uuid import UUID

import asyncpg

from bcn.persistence.runtime import ensure_schema_ready
from bcn.persistence.runtime import get_pool


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


def _simulation_params_from_report(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "limit": _coerce_int(report.get("limit"), 0),
        "since_days": _coerce_int(report.get("since_days"), 0),
        "include_text": bool(report.get("include_text", False)),
        "apply_critic_rewrites": bool(report.get("apply_critic_rewrites", False)),
        "reanalyze_items": bool(report.get("reanalyze_items", False)),
    }


def _simulation_report_metadata(report: dict[str, Any]) -> dict[str, Any]:
    payload = dict(report)
    payload["results"] = []
    return payload


async def create_simulation_run(
    *,
    params: dict[str, Any] | None = None,
    candidate_overrides: dict[str, Any] | None = None,
    report_path: str | None = None,
    source: str = "cli",
    notes: str | None = None,
) -> UUID:
    """Create a placeholder simulation run before replay work starts."""
    await ensure_simulation_tables()
    pool = await get_pool()
    run = await pool.fetchrow(
        """
        INSERT INTO simulation_runs (
            source,
            report_path,
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
            $3::jsonb,
            $4::jsonb,
            '{}'::jsonb,
            '{}'::jsonb,
            0,
            $5,
            'running'
        )
        RETURNING id
        """,
        source,
        report_path,
        json.dumps(params or {}, ensure_ascii=False, default=str),
        json.dumps(candidate_overrides or {}, ensure_ascii=False, default=str),
        notes,
    )
    return run["id"]


async def upsert_simulation_result(run_id: UUID, result: dict[str, Any]) -> None:
    """Persist one replay briefing result row for checkpointing/resume."""
    await ensure_simulation_tables()
    pool = await get_pool()
    briefing_id_raw = result.get("briefing_id")
    briefing_id = str(briefing_id_raw).strip() if briefing_id_raw else None
    await pool.execute(
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
        run_id,
        briefing_id if briefing_id else None,
        _coerce_iso_datetime(result.get("created_at")),
        _coerce_int(result.get("actual_score"), 0),
        _coerce_int(result.get("simulated_score"), 0),
        _coerce_int(result.get("delta"), 0),
        json.dumps(result, ensure_ascii=False, default=str),
    )


async def update_simulation_run_progress(
    run_id: UUID,
    report: dict[str, Any],
    *,
    report_path: str | None = None,
    notes: str | None = None,
) -> None:
    """Checkpoint one in-progress simulation run."""
    await ensure_simulation_tables()
    pool = await get_pool()
    summary = report.get("summary")
    if not isinstance(summary, dict):
        summary = {}
    candidate_overrides = report.get("candidate_overrides")
    if not isinstance(candidate_overrides, dict):
        candidate_overrides = {}
    await pool.execute(
        """
        UPDATE simulation_runs
        SET
            generated_at = $2,
            report_path = COALESCE($3, report_path),
            params = $4::jsonb,
            candidate_overrides = $5::jsonb,
            summary = $6::jsonb,
            report = $7::jsonb,
            count = $8,
            notes = COALESCE($9, notes),
            updated_at = NOW()
        WHERE id = $1
        """,
        run_id,
        _coerce_iso_datetime(report.get("generated_at")),
        report_path,
        json.dumps(_simulation_params_from_report(report), ensure_ascii=False, default=str),
        json.dumps(candidate_overrides, ensure_ascii=False, default=str),
        json.dumps(summary, ensure_ascii=False, default=str),
        json.dumps(_simulation_report_metadata(report), ensure_ascii=False, default=str),
        _coerce_int(report.get("count"), 0),
        notes,
    )


async def complete_simulation_run(
    run_id: UUID,
    report: dict[str, Any],
    *,
    report_path: str | None = None,
    notes: str | None = None,
) -> None:
    """Finalize a simulation run after all replay rows were checkpointed."""
    await update_simulation_run_progress(
        run_id,
        report,
        report_path=report_path,
        notes=notes,
    )
    await ensure_simulation_tables()
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE simulation_runs
        SET
            status = 'completed',
            error_message = NULL,
            finished_at = NOW(),
            updated_at = NOW()
        WHERE id = $1
        """,
        run_id,
    )


async def fail_simulation_run(
    run_id: UUID,
    *,
    error_message: str,
    notes: str | None = None,
) -> None:
    """Mark a simulation run as failed."""
    await ensure_simulation_tables()
    pool = await get_pool()
    message = str(error_message or "").strip()[:4000] or "simulation_failed"
    await pool.execute(
        """
        UPDATE simulation_runs
        SET
            notes = COALESCE($2, notes),
            status = 'failed',
            error_message = $3,
            finished_at = NOW(),
            updated_at = NOW()
        WHERE id = $1
        """,
        run_id,
        notes,
        message,
    )


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

    params = _simulation_params_from_report(report)
    generated_at = _coerce_iso_datetime(report.get("generated_at"))
    run_count = _coerce_int(report.get("count"), 0)
    candidate_overrides = report.get("candidate_overrides")
    if not isinstance(candidate_overrides, dict):
        candidate_overrides = {}

    run = await pool.fetchrow(
        """
        INSERT INTO simulation_runs (
            generated_at,
            source,
            report_path,
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
            $4::jsonb,
            $5::jsonb,
            $6::jsonb,
            $7::jsonb,
            $8,
            $9,
            'completed',
            NOW()
        )
        RETURNING id
        """,
        generated_at,
        source,
        report_path,
        json.dumps(params),
        json.dumps(candidate_overrides, ensure_ascii=False, default=str),
        json.dumps(summary),
        json.dumps(_simulation_report_metadata(report), ensure_ascii=False, default=str),
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
        SELECT
            id,
            created_at,
            generated_at,
            source,
            report_path,
            params,
            candidate_overrides,
            summary,
            report,
            count,
            notes,
            status,
            finished_at,
            error_message
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
    report_payload = _coerce_json_dict(run["report"])
    generated_at = run["generated_at"] or run["created_at"]

    report: dict[str, Any] = {
        **report_payload,
        "generated_at": generated_at.isoformat() if isinstance(generated_at, datetime) else None,
        "count": int(run["count"]),
        "limit": _coerce_int(params.get("limit"), 0),
        "since_days": _coerce_int(params.get("since_days"), 0),
        "include_text": bool(params.get("include_text", False)),
        "apply_critic_rewrites": bool(params.get("apply_critic_rewrites", False)),
        "reanalyze_items": bool(params.get("reanalyze_items", False)),
        "candidate_overrides": _coerce_json_dict(run["candidate_overrides"]),
        "summary": summary,
        "results": results,
        "db_run_id": str(run["id"]),
        "db_created_at": run["created_at"].isoformat(),
        "db_source": str(run["source"]),
        "db_status": str(run["status"] or "completed"),
    }
    if run["report_path"]:
        report["report_path"] = str(run["report_path"])
    if run["notes"]:
        report["notes"] = str(run["notes"])
    if run["finished_at"]:
        report["db_finished_at"] = run["finished_at"].isoformat()
    if run["error_message"]:
        report["db_error_message"] = str(run["error_message"])
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
    await update_evaluation_run_progress(
        run_id,
        report,
        report_path=report_path,
        notes=notes,
    )
    await ensure_evaluation_tables()
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE evaluation_runs
        SET
            status = 'completed',
            error_message = NULL,
            finished_at = NOW(),
            updated_at = NOW()
        WHERE id = $1
        """,
        run_id,
    )


async def update_evaluation_run_progress(
    run_id: UUID,
    report: dict[str, Any],
    *,
    report_path: str | None = None,
    notes: str | None = None,
) -> None:
    """Checkpoint a benchmark or shadow run while it is still executing."""
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
            status = 'running',
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


async def finalize_stale_evaluation_runs(*, stale_minutes: int) -> int:
    """Fail 'running' evaluation rows whose worker died without finalizing them.

    A job canceled by deadline or a worker restarted mid-run leaves the row
    'running' forever; anything without a progress update for the given
    window is closed out as failed.
    """
    minutes = max(1, int(stale_minutes))
    await ensure_evaluation_tables()
    pool = await get_pool()
    rows = await pool.fetch(
        """
        UPDATE evaluation_runs
        SET
            status = 'failed',
            error_message = COALESCE(error_message, 'stale_running_run'),
            summary = CASE
                WHEN summary IS NULL OR summary = '{}'::jsonb
                THEN '{"recommendation": "failed", "confidence": "low"}'::jsonb
                ELSE summary
            END,
            finished_at = NOW(),
            updated_at = NOW()
        WHERE status = 'running'
          AND updated_at < NOW() - make_interval(mins => $1)
        RETURNING id
        """,
        minutes,
    )
    return len(rows)


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
