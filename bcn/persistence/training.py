"""Persistence gateway for generation traces, reviews, and outcomes."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
import json
import logging
from typing import Any
from typing import Optional
from uuid import UUID

import asyncpg

from bcn.persistence.helpers import briefing_item_ids_sql
from bcn.persistence.runtime import ensure_schema_ready
from bcn.persistence.runtime import get_pool

logger = logging.getLogger(__name__)


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
    selection_trace: dict[str, Any] | None = None,
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
            selection_trace,
            git_sha,
            initial_draft
        )
        VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7::jsonb, $8::jsonb, $9::jsonb, $10, $11)
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
        json.dumps(selection_trace or {}, ensure_ascii=False, default=str),
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
    if (decision or "").strip().upper() == "BLOCKED":
        # Single choke point for every blocked slot regardless of cause
        # (quiet day, critic, verifier, editorial gate, exception).
        try:
            from bcn.common.alerts import consecutive_unpublished_scheduler_runs
            from bcn.common.alerts import quiet_streak_alert_due
            from bcn.common.alerts import send_operator_alert
            from bcn.common.config import Settings

            settings = Settings()
            streak = await consecutive_unpublished_scheduler_runs()
            threshold = int(
                getattr(settings, "alert_quiet_streak_threshold", 4) or 4
            )
            if quiet_streak_alert_due(streak, threshold):
                await send_operator_alert(
                    settings,
                    f"{streak} consecutive scheduled slots without a publish. "
                    f"Latest block: {(decision_reason or 'unknown')[:180]}",
                )
        except Exception:
            logger.exception("Unpublished-streak alert check failed")


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


async def get_briefing_review_context(briefing_id: UUID) -> asyncpg.Record | None:
    """Return briefing markdown and latest generation-run context for review workflows."""
    await ensure_training_tables()
    pool = await get_pool()
    return await pool.fetchrow(
        """
        SELECT
          b.id,
          b.created_at,
          b.distributed_at,
          b.status,
          b.content_markdown,
          b.cover_image_url,
          b.cover_image_prompt,
          gr.id AS run_id,
          gr.created_at AS run_created_at,
          gr.decision AS run_decision,
          gr.rewrite_count AS run_rewrite_count,
          gr.llm_model AS run_llm_model,
          gr.selected_items AS run_selected_items
        FROM briefings b
        LEFT JOIN LATERAL (
          SELECT id, created_at, decision, rewrite_count, llm_model, selected_items
          FROM generation_runs
          WHERE briefing_id = b.id
          ORDER BY created_at DESC
          LIMIT 1
        ) gr ON TRUE
        WHERE b.id = $1
        LIMIT 1
        """,
        briefing_id,
    )


async def has_ai_review(
    *,
    briefing_id: UUID,
    source: str | None = None,
) -> bool:
    """Return whether the briefing already has a stored AI review."""
    await ensure_training_tables()
    pool = await get_pool()
    where = ["briefing_id = $1"]
    params: list[object] = [briefing_id]
    if source is not None:
        params.append(str(source).strip())
        where.append(f"source = ${len(params)}")
    row = await pool.fetchrow(
        f"""
        SELECT EXISTS(
            SELECT 1
            FROM briefing_ai_reviews
            WHERE {" AND ".join(where)}
        ) AS present
        """,
        *params,
    )
    return bool(row["present"]) if row else False


async def get_distributed_briefings_without_ai_review(
    *,
    limit: int = 25,
    source: str = "auto_distribution",
) -> list[asyncpg.Record]:
    """Return recent distributed briefings missing an AI review from the given source."""
    await ensure_training_tables()
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT
            b.id,
            b.created_at,
            b.distributed_at
        FROM briefings b
        WHERE b.status = 'DISTRIBUTED'
          AND NOT EXISTS (
              SELECT 1
              FROM briefing_ai_reviews ar
              WHERE ar.briefing_id = b.id
                AND ar.source = $2
          )
        ORDER BY b.created_at DESC
        LIMIT $1
        """,
        max(1, int(limit)),
        str(source or "auto_distribution").strip(),
    )


async def insert_ai_review(
    *,
    briefing_id: UUID,
    reviewer_provider: str,
    reviewer_model: str,
    decision: str,
    issue_tags: list[str] | None = None,
    edited_markdown: str | None = None,
    notes: str | None = None,
    raw_response: dict[str, Any] | None = None,
    reasoning_effort: str | None = None,
    source: str = "auto",
    run_id: UUID | None = None,
) -> UUID:
    """Store one AI review row tied to a briefing/run."""
    await ensure_training_tables()
    pool = await get_pool()
    tags = [str(tag).strip() for tag in (issue_tags or []) if str(tag).strip()]
    resolved_run_id = run_id
    if resolved_run_id is None:
        run_row = await pool.fetchrow(
            """
            SELECT id
            FROM generation_runs
            WHERE briefing_id = $1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            briefing_id,
        )
        resolved_run_id = run_row["id"] if run_row else None
    row = await pool.fetchrow(
        """
        INSERT INTO briefing_ai_reviews (
            briefing_id,
            run_id,
            source,
            reviewer_provider,
            reviewer_model,
            reasoning_effort,
            decision,
            issue_tags,
            edited_markdown,
            notes,
            raw_response
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::text[], $9, $10, $11::jsonb)
        RETURNING id
        """,
        briefing_id,
        resolved_run_id,
        str(source or "auto").strip(),
        str(reviewer_provider or "openai").strip(),
        str(reviewer_model or "").strip(),
        str(reasoning_effort).strip() if reasoning_effort else None,
        str(decision or "").strip().lower(),
        tags,
        edited_markdown,
        notes,
        json.dumps(raw_response or {}, ensure_ascii=False, default=str),
    )
    return row["id"]


async def get_review_queue(
    *,
    limit: int = 20,
    only_unreviewed: bool = False,
) -> list[asyncpg.Record]:
    """List recent briefings with review summary information."""
    await ensure_training_tables()
    item_ids_sql = briefing_item_ids_sql("b")
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


async def get_ai_reviews(
    *,
    briefing_ids: list[UUID] | None = None,
    run_ids: list[UUID] | None = None,
    limit: int = 0,
) -> list[asyncpg.Record]:
    """Fetch AI review rows for export/reporting."""
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
        "SELECT * FROM briefing_ai_reviews "
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


async def get_generation_rounds_for_runs(
    run_ids: list[UUID],
) -> list[asyncpg.Record]:
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
