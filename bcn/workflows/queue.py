"""Durable workflow queue, scheduler enqueueing, and lane workers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import logging
import os
import socket
from typing import Any
from uuid import UUID
from uuid import uuid4

from bcn.common.config import Settings
from bcn.evaluation.service import execute_benchmark_lane
from bcn.evaluation.service import execute_simulation_lane
from bcn.persistence.evaluation import create_evaluation_run
from bcn.persistence.evaluation import create_simulation_run
from bcn.persistence.workflow_jobs import cancel_expired_workflow_jobs
from bcn.persistence.workflow_jobs import claim_next_workflow_job
from bcn.persistence.workflow_jobs import complete_workflow_job
from bcn.persistence.workflow_jobs import create_workflow_job
from bcn.persistence.workflow_jobs import fail_workflow_job
from bcn.persistence.workflow_jobs import reclaim_expired_workflow_job_leases
from bcn.persistence.workflow_jobs import renew_workflow_job_lease
from bcn.persistence.workflow_jobs import requeue_workflow_job
from bcn.persistence.workflow_jobs import update_workflow_job_progress
from bcn.workflows.ai_review import run_briefing_ai_review
from bcn.workflows.catalog import ScheduledWorkflowDefinition
from bcn.workflows.catalog import WorkflowStepDefinition
from bcn.workflows.catalog import get_scheduled_workflow_definition
from bcn.workflows.execution import execute_workflow_steps
from bcn.workflows.execution import serialize_step_state
from bcn.workflows.runtime import WorkflowRuntime

logger = logging.getLogger("bcn")

JOB_TYPE_SCHEDULED_WORKFLOW = "scheduled_workflow"
JOB_TYPE_EVALUATION_BENCHMARK = "evaluation_benchmark"
JOB_TYPE_EVALUATION_SIMULATION = "evaluation_simulation"
JOB_TYPE_BRIEFING_AI_REVIEW = "briefing_ai_review"
DEFAULT_WORKER_LANES = ("publish", "collection", "analysis", "evaluation")
_LANE_LEASE_ATTRS: dict[str, tuple[str, ...]] = {
    "publish": ("workflow_job_publish_lease_seconds", "workflow_job_default_lease_seconds"),
    "collection": (
        "workflow_job_collection_lease_seconds",
        "workflow_job_default_lease_seconds",
    ),
    "analysis": (
        "workflow_job_analysis_lease_seconds",
        "workflow_job_default_lease_seconds",
    ),
    "evaluation": (
        "workflow_job_evaluation_lease_seconds",
        "workflow_job_default_lease_seconds",
    ),
}
_LANE_RETRY_BASE_ATTRS: dict[str, tuple[str, ...]] = {
    "publish": (
        "workflow_job_publish_retry_base_delay_seconds",
        "workflow_job_retry_base_delay_seconds",
    ),
    "collection": (
        "workflow_job_collection_retry_base_delay_seconds",
        "workflow_job_retry_base_delay_seconds",
    ),
    "analysis": (
        "workflow_job_analysis_retry_base_delay_seconds",
        "workflow_job_retry_base_delay_seconds",
    ),
    "evaluation": (
        "workflow_job_evaluation_retry_base_delay_seconds",
        "workflow_job_retry_base_delay_seconds",
    ),
}
_LANE_RETRY_MAX_ATTRS: dict[str, tuple[str, ...]] = {
    "publish": (
        "workflow_job_publish_retry_max_delay_seconds",
        "workflow_job_retry_max_delay_seconds",
    ),
    "collection": (
        "workflow_job_collection_retry_max_delay_seconds",
        "workflow_job_retry_max_delay_seconds",
    ),
    "analysis": (
        "workflow_job_analysis_retry_max_delay_seconds",
        "workflow_job_retry_max_delay_seconds",
    ),
    "evaluation": (
        "workflow_job_evaluation_retry_max_delay_seconds",
        "workflow_job_retry_max_delay_seconds",
    ),
}
_LANE_DEADLINE_ATTRS: dict[str, tuple[str, ...]] = {
    "publish": ("workflow_job_publish_deadline_seconds",),
    "collection": ("workflow_job_collection_deadline_seconds",),
    "analysis": ("workflow_job_analysis_deadline_seconds",),
    "evaluation": ("workflow_job_evaluation_deadline_seconds",),
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_lanes(lanes: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    if not lanes:
        return DEFAULT_WORKER_LANES
    normalized: list[str] = []
    seen: set[str] = set()
    for lane in lanes:
        value = str(lane or "").strip().lower()
        if not value or value in seen:
            continue
        normalized.append(value)
        seen.add(value)
    return tuple(normalized) or DEFAULT_WORKER_LANES


def _lane_value(
    settings: Settings,
    lane: str,
    mapping: dict[str, tuple[str, ...]],
    *,
    minimum: int,
    fallback: int,
) -> int:
    normalized_lane = str(lane or "").strip().lower()
    for attr in mapping.get(normalized_lane, ()):
        raw_value = getattr(settings, attr, None)
        if raw_value is None:
            continue
        return max(minimum, int(raw_value))
    return max(minimum, int(fallback))


def _lease_seconds_for_lane(settings: Settings, lane: str) -> int:
    return _lane_value(
        settings,
        lane,
        _LANE_LEASE_ATTRS,
        minimum=60,
        fallback=int(settings.workflow_job_default_lease_seconds),
    )


def _worker_id(label: str | None = None) -> str:
    host = socket.gethostname()
    pid = os.getpid()
    suffix = str(label or "").strip()
    nonce = uuid4().hex[:8]
    if suffix:
        return f"{host}:{pid}:{suffix}:{nonce}"
    return f"{host}:{pid}:{nonce}"


def _retry_delay_seconds(settings: Settings, lane: str, attempt_count: int) -> int:
    base = _lane_value(
        settings,
        lane,
        _LANE_RETRY_BASE_ATTRS,
        minimum=1,
        fallback=int(settings.workflow_job_retry_base_delay_seconds),
    )
    maximum = _lane_value(
        settings,
        lane,
        _LANE_RETRY_MAX_ATTRS,
        minimum=base,
        fallback=max(base, int(settings.workflow_job_retry_max_delay_seconds)),
    )
    exponent = max(0, int(attempt_count) - 1)
    return min(maximum, base * (2**exponent))


def _lane_deadline_at(settings: Settings, lane: str) -> datetime | None:
    normalized_lane = str(lane or "").strip().lower()
    attrs = _LANE_DEADLINE_ATTRS.get(normalized_lane)
    if not attrs:
        return None
    deadline_seconds = _lane_value(
        settings,
        normalized_lane,
        _LANE_DEADLINE_ATTRS,
        minimum=1,
        fallback=0,
    )
    return _utc_now() + timedelta(seconds=deadline_seconds)


def _workflow_deadline_at(
    settings: Settings,
    definition: ScheduledWorkflowDefinition,
) -> datetime | None:
    deadline_seconds = definition.deadline_seconds(settings)
    if deadline_seconds is not None:
        return _utc_now() + timedelta(seconds=deadline_seconds)
    return _lane_deadline_at(settings, definition.lane)


def _build_scheduled_job_payload(
    definition: ScheduledWorkflowDefinition,
) -> dict[str, Any]:
    return definition.to_job_payload()


async def enqueue_scheduled_workflow_job(
    settings: Settings,
    definition: ScheduledWorkflowDefinition,
    *,
    source: str = "scheduler",
    notes: str | None = None,
) -> UUID:
    """Create one queued scheduled workflow job."""
    return await create_workflow_job(
        lane=definition.lane,
        priority=definition.priority,
        job_type=JOB_TYPE_SCHEDULED_WORKFLOW,
        source=source,
        workflow_id=definition.workflow_id,
        max_attempts=definition.max_attempts,
        lease_duration_seconds=_lease_seconds_for_lane(settings, definition.lane),
        payload=_build_scheduled_job_payload(definition),
        notes=notes,
        deadline_at=_workflow_deadline_at(settings, definition),
    )


async def enqueue_benchmark_job(
    settings: Settings,
    *,
    cases_path: str,
    candidate_overrides_path: str | None = None,
    output_path: str | None = "benchmark_report.json",
    include_text: bool = False,
    source: str = "cli",
    notes: str | None = None,
) -> UUID:
    """Queue one benchmark lane job for an evaluation worker."""
    return await create_workflow_job(
        lane="evaluation",
        priority=15,
        job_type=JOB_TYPE_EVALUATION_BENCHMARK,
        source=source,
        max_attempts=2,
        lease_duration_seconds=max(
            60, int(settings.workflow_job_evaluation_lease_seconds)
        ),
        payload={
            "cases_path": cases_path,
            "candidate_overrides_path": candidate_overrides_path,
            "output_path": output_path,
            "include_text": bool(include_text),
            "source": source,
            "notes": notes,
        },
        notes=notes,
        deadline_at=_lane_deadline_at(settings, "evaluation"),
    )


async def enqueue_simulation_job(
    settings: Settings,
    *,
    limit: int = 30,
    since_days: int = 90,
    candidate_overrides_path: str | None = None,
    output_path: str = "simulation_report.json",
    include_text: bool = False,
    with_critic_rewrites: bool = False,
    reanalyze_items: bool = False,
    source: str = "cli",
    notes: str | None = None,
) -> UUID:
    """Queue one replay/simulation lane job for an evaluation worker."""
    return await create_workflow_job(
        lane="evaluation",
        priority=15,
        job_type=JOB_TYPE_EVALUATION_SIMULATION,
        source=source,
        max_attempts=2,
        lease_duration_seconds=max(
            60, int(settings.workflow_job_evaluation_lease_seconds)
        ),
        payload={
            "limit": max(0, int(limit)),
            "since_days": max(0, int(since_days)),
            "candidate_overrides_path": candidate_overrides_path,
            "output_path": output_path,
            "include_text": bool(include_text),
            "with_critic_rewrites": bool(with_critic_rewrites),
            "reanalyze_items": bool(reanalyze_items),
            "source": source,
            "notes": notes,
        },
        notes=notes,
        deadline_at=_lane_deadline_at(settings, "evaluation"),
    )


async def enqueue_ai_review_job(
    settings: Settings,
    *,
    briefing_id: UUID,
    source: str = "auto_distribution",
    notes: str | None = None,
) -> UUID:
    """Queue one automatic AI review job for a distributed briefing."""
    return await create_workflow_job(
        lane="evaluation",
        priority=18,
        job_type=JOB_TYPE_BRIEFING_AI_REVIEW,
        source=source,
        dedupe_key=f"ai_review:auto:{briefing_id}",
        max_attempts=2,
        lease_duration_seconds=max(
            60, int(settings.workflow_job_evaluation_lease_seconds)
        ),
        payload={
            "briefing_id": str(briefing_id),
            "source": source,
            "notes": notes,
        },
        notes=notes,
        deadline_at=_lane_deadline_at(settings, "evaluation"),
    )


async def _lease_heartbeat(
    job_id: UUID,
    *,
    worker_id: str,
    state_getter: Callable[[], dict[str, Any]],
    refresh_seconds: int,
) -> None:
    while True:
        await asyncio.sleep(max(5, int(refresh_seconds)))
        await renew_workflow_job_lease(
            job_id,
            worker_id=worker_id,
            state=dict(state_getter() or {}),
        )


async def _execute_scheduled_workflow_job(
    settings: Settings,
    runtime: WorkflowRuntime,
    job: dict[str, Any],
    *,
    worker_id: str,
) -> dict[str, Any]:
    payload = dict(job.get("payload") or {})
    workflow_id = str(payload.get("workflow_id") or job.get("workflow_id") or "").strip()
    payload_steps = payload.get("steps")
    if isinstance(payload_steps, list):
        steps = tuple(
            WorkflowStepDefinition.from_payload(step)
            for step in payload_steps
            if isinstance(step, dict)
        )
    else:
        definition = get_scheduled_workflow_definition(workflow_id)
        if definition is None:
            raise ValueError(f"Unknown scheduled workflow: {workflow_id}")
        steps = definition.steps

    current_state = job.get("state") if isinstance(job.get("state"), dict) else {}
    job["state"] = current_state
    start_step_index = max(0, int(current_state.get("next_step_index", 0) or 0))
    step_state = dict(current_state.get("step_state") or {})

    async def _on_step_complete(
        step_index: int,
        step: WorkflowStepDefinition,
        state: dict[str, Any],
    ) -> None:
        current_state["next_step_index"] = step_index + 1
        current_state["step_state"] = state
        await update_workflow_job_progress(
            job["id"],
            worker_id=worker_id,
            state=current_state,
            attempt_id=job.get("attempt_id"),
            artifact_key=f"step:{step.step_id}",
            artifact_type="workflow_step",
            artifact_payload={
                "step_id": step.step_id,
                "step_index": step_index,
                "state": state,
            },
        )

    final_state = await execute_workflow_steps(
        runtime,
        workflow_id=workflow_id,
        steps=steps,
        initial_state=step_state,
        start_step_index=start_step_index,
        on_step_complete=_on_step_complete,
    )
    serialized_final_state = serialize_step_state(final_state)
    current_state["next_step_index"] = len(steps)
    current_state["step_state"] = serialized_final_state
    return {
        "workflow_id": workflow_id,
        "completed_steps": len(steps),
        "state": serialized_final_state,
    }


async def _ensure_benchmark_run(
    job: dict[str, Any],
    payload: dict[str, Any],
    *,
    worker_id: str,
) -> tuple[UUID, dict[str, Any]]:
    current_state = job.get("state") if isinstance(job.get("state"), dict) else {}
    job["state"] = current_state
    run_id = current_state.get("run_id")
    if run_id:
        return UUID(str(run_id)), current_state
    created_run_id = await create_evaluation_run(
        lane="benchmark",
        source=str(payload.get("source") or job.get("source") or "worker"),
        report_path=str(payload.get("output_path") or "").strip() or None,
        pack_path=str(payload.get("cases_path") or "").strip() or None,
        notes=str(payload.get("notes") or "").strip() or None,
    )
    current_state["run_id"] = str(created_run_id)
    current_state["next_case_index"] = max(
        0,
        int(current_state.get("next_case_index", 0) or 0),
    )
    await update_workflow_job_progress(
        job["id"],
        worker_id=worker_id,
        state=current_state,
        attempt_id=job.get("attempt_id"),
    )
    return created_run_id, current_state


async def _execute_benchmark_job(
    settings: Settings,
    job: dict[str, Any],
    *,
    worker_id: str,
) -> dict[str, Any]:
    payload = dict(job.get("payload") or {})
    run_id, current_state = await _ensure_benchmark_run(
        job,
        payload,
        worker_id=worker_id,
    )

    async def _on_progress(next_case_index: int, partial_report: dict[str, Any]) -> None:
        current_state["run_id"] = str(partial_report.get("db_run_id") or run_id)
        current_state["next_case_index"] = max(0, int(next_case_index))
        await update_workflow_job_progress(
            job["id"],
            worker_id=worker_id,
            state=current_state,
            attempt_id=job.get("attempt_id"),
            artifact_key=f"benchmark:{next_case_index}",
            artifact_type="evaluation_progress",
            artifact_payload={
                "next_case_index": next_case_index,
                "summary": dict(partial_report.get("summary") or {}),
                "count": int(partial_report.get("count", 0) or 0),
                "db_run_id": current_state["run_id"],
            },
        )

    report = await execute_benchmark_lane(
        settings,
        cases_path=str(payload.get("cases_path") or ""),
        candidate_overrides_path=(
            str(payload.get("candidate_overrides_path")).strip()
            if payload.get("candidate_overrides_path")
            else None
        ),
        output_path=(
            str(payload.get("output_path")).strip()
            if payload.get("output_path")
            else None
        ),
        include_text=bool(payload.get("include_text", False)),
        store_db=True,
        source=str(payload.get("source") or job.get("source") or "worker"),
        notes=str(payload.get("notes") or "").strip() or None,
        run_id=run_id,
        start_case_index=max(0, int(current_state.get("next_case_index", 0) or 0)),
        progress_callback=_on_progress,
        manage_pool=False,
    )
    current_state["run_id"] = str(run_id)
    current_state["next_case_index"] = int(report.get("count", 0) or 0)
    await update_workflow_job_progress(
        job["id"],
        worker_id=worker_id,
        state=current_state,
        attempt_id=job.get("attempt_id"),
        artifact_key="benchmark:final",
        artifact_type="evaluation_progress",
        artifact_payload={
            "summary": dict(report.get("summary") or {}),
            "count": int(report.get("count", 0) or 0),
            "db_run_id": str(run_id),
        },
    )
    return report


async def _ensure_simulation_run(
    job: dict[str, Any],
    payload: dict[str, Any],
    *,
    worker_id: str,
) -> tuple[UUID, dict[str, Any]]:
    current_state = job.get("state") if isinstance(job.get("state"), dict) else {}
    job["state"] = current_state
    run_id = current_state.get("run_id")
    if run_id:
        return UUID(str(run_id)), current_state
    created_run_id = await create_simulation_run(
        params={
            "limit": max(0, int(payload.get("limit", 0) or 0)),
            "since_days": max(0, int(payload.get("since_days", 0) or 0)),
            "include_text": bool(payload.get("include_text", False)),
            "apply_critic_rewrites": bool(payload.get("with_critic_rewrites", False)),
            "reanalyze_items": bool(payload.get("reanalyze_items", False)),
        },
        report_path=str(payload.get("output_path") or "").strip() or None,
        source=str(payload.get("source") or job.get("source") or "worker"),
        notes=str(payload.get("notes") or "").strip() or None,
    )
    current_state["run_id"] = str(created_run_id)
    current_state["next_briefing_index"] = max(
        0,
        int(current_state.get("next_briefing_index", 0) or 0),
    )
    await update_workflow_job_progress(
        job["id"],
        worker_id=worker_id,
        state=current_state,
        attempt_id=job.get("attempt_id"),
    )
    return created_run_id, current_state


async def _execute_simulation_job(
    settings: Settings,
    job: dict[str, Any],
    *,
    worker_id: str,
) -> dict[str, Any]:
    payload = dict(job.get("payload") or {})
    run_id, current_state = await _ensure_simulation_run(
        job,
        payload,
        worker_id=worker_id,
    )

    async def _on_progress(
        next_briefing_index: int,
        partial_report: dict[str, Any],
    ) -> None:
        current_state["run_id"] = str(partial_report.get("db_run_id") or run_id)
        current_state["next_briefing_index"] = max(0, int(next_briefing_index))
        await update_workflow_job_progress(
            job["id"],
            worker_id=worker_id,
            state=current_state,
            attempt_id=job.get("attempt_id"),
            artifact_key=f"simulation:{next_briefing_index}",
            artifact_type="evaluation_progress",
            artifact_payload={
                "next_briefing_index": next_briefing_index,
                "summary": dict(partial_report.get("summary") or {}),
                "count": int(partial_report.get("count", 0) or 0),
                "db_run_id": current_state["run_id"],
            },
        )

    report = await execute_simulation_lane(
        settings,
        limit=max(0, int(payload.get("limit", 0) or 0)),
        since_days=max(0, int(payload.get("since_days", 0) or 0)),
        candidate_overrides_path=(
            str(payload.get("candidate_overrides_path")).strip()
            if payload.get("candidate_overrides_path")
            else None
        ),
        output_path=str(payload.get("output_path") or "simulation_report.json"),
        include_text=bool(payload.get("include_text", False)),
        with_critic_rewrites=bool(payload.get("with_critic_rewrites", False)),
        reanalyze_items=bool(payload.get("reanalyze_items", False)),
        store_db=True,
        source=str(payload.get("source") or job.get("source") or "worker"),
        run_id=run_id,
        start_briefing_index=max(
            0,
            int(current_state.get("next_briefing_index", 0) or 0),
        ),
        progress_callback=_on_progress,
        manage_pool=False,
    )
    current_state["run_id"] = str(run_id)
    current_state["next_briefing_index"] = int(report.get("count", 0) or 0)
    await update_workflow_job_progress(
        job["id"],
        worker_id=worker_id,
        state=current_state,
        attempt_id=job.get("attempt_id"),
        artifact_key="simulation:final",
        artifact_type="evaluation_progress",
        artifact_payload={
            "summary": dict(report.get("summary") or {}),
            "count": int(report.get("count", 0) or 0),
            "db_run_id": str(run_id),
        },
    )
    return report


async def _execute_ai_review_job(
    settings: Settings,
    job: dict[str, Any],
    *,
    worker_id: str,
) -> dict[str, Any]:
    payload = dict(job.get("payload") or {})
    briefing_id_raw = str(payload.get("briefing_id") or "").strip()
    if not briefing_id_raw:
        raise ValueError("AI review job missing briefing_id")
    source = str(payload.get("source") or job.get("source") or "auto_distribution").strip()
    result = await run_briefing_ai_review(
        settings,
        briefing_id=UUID(briefing_id_raw),
        source=source,
    )
    await update_workflow_job_progress(
        job["id"],
        worker_id=worker_id,
        state={
            "briefing_id": briefing_id_raw,
            "source": source,
            "result": result,
        },
        attempt_id=job.get("attempt_id"),
        artifact_key="ai_review:final",
        artifact_type="ai_review",
        artifact_payload=result,
    )
    return result


async def execute_claimed_workflow_job(
    settings: Settings,
    runtime: WorkflowRuntime,
    job: dict[str, Any],
    *,
    worker_id: str,
) -> dict[str, Any]:
    """Execute one claimed durable workflow job."""
    job_type = str(job.get("job_type") or "").strip()
    if job_type == JOB_TYPE_SCHEDULED_WORKFLOW:
        return await _execute_scheduled_workflow_job(
            settings,
            runtime,
            job,
            worker_id=worker_id,
        )
    if job_type == JOB_TYPE_EVALUATION_BENCHMARK:
        return await _execute_benchmark_job(settings, job, worker_id=worker_id)
    if job_type == JOB_TYPE_EVALUATION_SIMULATION:
        return await _execute_simulation_job(settings, job, worker_id=worker_id)
    if job_type == JOB_TYPE_BRIEFING_AI_REVIEW:
        return await _execute_ai_review_job(settings, job, worker_id=worker_id)
    raise ValueError(f"Unsupported workflow job type: {job_type}")


async def _run_one_claimed_job(
    settings: Settings,
    runtime: WorkflowRuntime,
    job: dict[str, Any],
    *,
    worker_id: str,
) -> None:
    current_state = job.get("state") if isinstance(job.get("state"), dict) else {}
    job["state"] = current_state
    heartbeat = asyncio.create_task(
        _lease_heartbeat(
            job["id"],
            worker_id=worker_id,
            state_getter=lambda: current_state,
            refresh_seconds=max(5, int(settings.workflow_job_lease_refresh_seconds)),
        )
    )
    try:
        result = await execute_claimed_workflow_job(
            settings,
            runtime,
            job,
            worker_id=worker_id,
        )
        await complete_workflow_job(
            job["id"],
            worker_id=worker_id,
            result=result,
            attempt_id=job.get("attempt_id"),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception(
            "Workflow job failed: id=%s lane=%s type=%s attempt=%s/%s",
            job.get("id"),
            job.get("lane"),
            job.get("job_type"),
            job.get("attempt_count"),
            job.get("max_attempts"),
        )
        deadline_at = job.get("deadline_at")
        deadline_passed = bool(deadline_at and deadline_at <= _utc_now())
        if deadline_passed:
            await fail_workflow_job(
                job["id"],
                worker_id=worker_id,
                error_message=f"deadline_exceeded:{type(exc).__name__}:{exc}",
                state=current_state,
                attempt_id=job.get("attempt_id"),
                canceled=True,
            )
            return
        if int(job.get("attempt_count", 0) or 0) >= int(job.get("max_attempts", 1) or 1):
            await fail_workflow_job(
                job["id"],
                worker_id=worker_id,
                error_message=str(exc),
                state=current_state,
                attempt_id=job.get("attempt_id"),
            )
            return
        await requeue_workflow_job(
            job["id"],
            worker_id=worker_id,
            error_message=str(exc),
            delay_seconds=_retry_delay_seconds(
                settings,
                str(job.get("lane") or ""),
                int(job.get("attempt_count", 0) or 0),
            ),
            state=current_state,
            attempt_id=job.get("attempt_id"),
        )
    finally:
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass


async def run_worker(
    settings: Settings,
    runtime: WorkflowRuntime,
    *,
    lanes: tuple[str, ...] | list[str] | None = None,
    emit: Callable[[str], None] | None = None,
    worker_name: str | None = None,
    once: bool = False,
) -> None:
    """Run one or more lane-scoped worker loops."""
    normalized_lanes = _normalize_lanes(lanes)

    def _emit(message: str) -> None:
        if emit is not None:
            emit(message)

    async def _lane_loop(lane: str, lane_worker_id: str) -> None:
        processed_once = False
        while True:
            await reclaim_expired_workflow_job_leases()
            await cancel_expired_workflow_jobs()
            job = await claim_next_workflow_job(lanes=[lane], worker_id=lane_worker_id)
            if not job:
                if once:
                    return
                await asyncio.sleep(max(1, int(settings.workflow_job_poll_interval_seconds)))
                continue
            _emit(
                f"Worker claimed job {job['id']} lane={job['lane']} type={job['job_type']} attempt={job['attempt_count']}"
            )
            await _run_one_claimed_job(
                settings,
                runtime,
                job,
                worker_id=lane_worker_id,
            )
            processed_once = True
            if once and processed_once:
                return

    async with asyncio.TaskGroup() as tg:
        for lane in normalized_lanes:
            tg.create_task(_lane_loop(lane, _worker_id(f"{worker_name or 'worker'}:{lane}")))


__all__ = [
    "DEFAULT_WORKER_LANES",
    "JOB_TYPE_BRIEFING_AI_REVIEW",
    "JOB_TYPE_EVALUATION_BENCHMARK",
    "JOB_TYPE_EVALUATION_SIMULATION",
    "JOB_TYPE_SCHEDULED_WORKFLOW",
    "enqueue_ai_review_job",
    "enqueue_benchmark_job",
    "enqueue_scheduled_workflow_job",
    "enqueue_simulation_job",
    "execute_claimed_workflow_job",
    "run_worker",
]
