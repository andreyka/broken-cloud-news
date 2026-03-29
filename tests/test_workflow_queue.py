from __future__ import annotations

from datetime import datetime
from datetime import timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from bcn.common.config import Settings
from bcn.contracts.workflow import WriterHandoff
from bcn.contracts.workflow import WriterHandoffResult
from bcn.workflows.catalog import get_scheduled_workflow_definition
from bcn.workflows.queue import execute_claimed_workflow_job
from bcn.workflows.queue import enqueue_ai_review_job
from bcn.workflows.queue import enqueue_benchmark_job
from bcn.workflows.queue import enqueue_scheduled_workflow_job
from bcn.workflows.queue import enqueue_simulation_job
from bcn.workflows.queue import JOB_TYPE_BRIEFING_AI_REVIEW
from bcn.workflows.queue import JOB_TYPE_SCHEDULED_WORKFLOW
from bcn.workflows.runtime import WorkflowRuntime


@pytest.mark.asyncio
async def test_enqueue_scheduled_workflow_job_uses_definition_queue_policy(monkeypatch):
    create_mock = AsyncMock(return_value=uuid4())
    monkeypatch.setattr("bcn.workflows.queue.create_workflow_job", create_mock)
    settings = Settings(
        shadow_enabled=True,
        shadow_minutes_before_publish=45,
    )
    definition = get_scheduled_workflow_definition("regular_daily_briefing_shadow")
    assert definition is not None

    job_id = await enqueue_scheduled_workflow_job(settings, definition)

    assert job_id is not None
    kwargs = create_mock.await_args.kwargs
    assert kwargs["lane"] == "evaluation"
    assert kwargs["priority"] == 20
    assert kwargs["job_type"] == "scheduled_workflow"
    assert kwargs["max_attempts"] == 1
    assert kwargs["deadline_at"] is not None


@pytest.mark.asyncio
async def test_enqueue_collection_workflow_job_uses_lane_deadline_and_lease(monkeypatch):
    create_mock = AsyncMock(return_value=uuid4())
    monkeypatch.setattr("bcn.workflows.queue.create_workflow_job", create_mock)
    settings = Settings(
        workflow_job_collection_deadline_seconds=3210,
        workflow_job_collection_lease_seconds=654,
    )
    definition = get_scheduled_workflow_definition("ghsa_collector")
    assert definition is not None

    before = datetime.now(timezone.utc)
    await enqueue_scheduled_workflow_job(settings, definition)
    after = datetime.now(timezone.utc)

    kwargs = create_mock.await_args.kwargs
    assert kwargs["lane"] == "collection"
    assert kwargs["lease_duration_seconds"] == 654
    deadline_at = kwargs["deadline_at"]
    assert deadline_at is not None
    min_expected = int((deadline_at - before).total_seconds())
    max_expected = int((deadline_at - after).total_seconds())
    assert 3208 <= min_expected <= 3212
    assert 3208 <= max_expected <= 3212


@pytest.mark.asyncio
async def test_enqueue_analysis_workflow_job_uses_lane_deadline_and_lease(monkeypatch):
    create_mock = AsyncMock(return_value=uuid4())
    monkeypatch.setattr("bcn.workflows.queue.create_workflow_job", create_mock)
    settings = Settings(
        workflow_job_analysis_deadline_seconds=1800,
        workflow_job_analysis_lease_seconds=777,
    )
    definition = get_scheduled_workflow_definition("analyst")
    assert definition is not None

    before = datetime.now(timezone.utc)
    await enqueue_scheduled_workflow_job(settings, definition)
    after = datetime.now(timezone.utc)

    kwargs = create_mock.await_args.kwargs
    assert kwargs["lane"] == "analysis"
    assert kwargs["lease_duration_seconds"] == 777
    deadline_at = kwargs["deadline_at"]
    assert deadline_at is not None
    min_expected = int((deadline_at - before).total_seconds())
    max_expected = int((deadline_at - after).total_seconds())
    assert 1798 <= min_expected <= 1802
    assert 1798 <= max_expected <= 1802


@pytest.mark.asyncio
async def test_enqueue_evaluation_jobs_use_lane_deadline_and_lease(monkeypatch):
    create_mock = AsyncMock(side_effect=[uuid4(), uuid4()])
    monkeypatch.setattr("bcn.workflows.queue.create_workflow_job", create_mock)
    settings = Settings(
        workflow_job_evaluation_deadline_seconds=28800,
        workflow_job_evaluation_lease_seconds=2222,
    )

    before = datetime.now(timezone.utc)
    await enqueue_benchmark_job(settings, cases_path="benchmark_packs/core_v1.json")
    await enqueue_simulation_job(settings, limit=5, since_days=7)
    after = datetime.now(timezone.utc)

    benchmark_kwargs = create_mock.await_args_list[0].kwargs
    simulation_kwargs = create_mock.await_args_list[1].kwargs
    assert benchmark_kwargs["lane"] == "evaluation"
    assert benchmark_kwargs["lease_duration_seconds"] == 2222
    assert simulation_kwargs["lane"] == "evaluation"
    assert simulation_kwargs["lease_duration_seconds"] == 2222
    for kwargs in (benchmark_kwargs, simulation_kwargs):
        deadline_at = kwargs["deadline_at"]
        assert deadline_at is not None
        min_expected = int((deadline_at - before).total_seconds())
        max_expected = int((deadline_at - after).total_seconds())
        assert 28798 <= min_expected <= 28802
        assert 28798 <= max_expected <= 28802


@pytest.mark.asyncio
async def test_enqueue_ai_review_job_uses_evaluation_lane_and_dedupe_key(monkeypatch):
    create_mock = AsyncMock(return_value=uuid4())
    monkeypatch.setattr("bcn.workflows.queue.create_workflow_job", create_mock)
    settings = Settings(
        workflow_job_evaluation_deadline_seconds=7200,
        workflow_job_evaluation_lease_seconds=900,
    )
    briefing_id = uuid4()

    await enqueue_ai_review_job(settings, briefing_id=briefing_id)

    kwargs = create_mock.await_args.kwargs
    assert kwargs["lane"] == "evaluation"
    assert kwargs["job_type"] == "briefing_ai_review"
    assert kwargs["dedupe_key"] == f"ai_review:auto:{briefing_id}"
    assert kwargs["lease_duration_seconds"] == 900
    assert kwargs["payload"]["briefing_id"] == str(briefing_id)


@pytest.mark.asyncio
async def test_execute_claimed_workflow_job_serializes_workflow_state(monkeypatch):
    update_mock = AsyncMock()
    execute_mock = AsyncMock(
        return_value={
            "writer_handoff": WriterHandoffResult(
                handoff=WriterHandoff(
                    mode="regular_daily_briefing",
                    decision="publish",
                    briefing_id=uuid4(),
                    item_count=3,
                ),
                human_message="ready",
            )
        }
    )
    monkeypatch.setattr("bcn.workflows.queue.update_workflow_job_progress", update_mock)
    monkeypatch.setattr("bcn.workflows.queue.execute_workflow_steps", execute_mock)

    job = {
        "id": uuid4(),
        "job_type": JOB_TYPE_SCHEDULED_WORKFLOW,
        "payload": {
            "workflow_id": "regular_daily_briefing",
            "steps": [
                {
                    "step_id": "generate_briefing",
                    "component": "writer",
                    "operation": "generate_release_candidate",
                    "args": {"mode": "regular_daily_briefing"},
                }
            ],
        },
        "state": {
            "next_step_index": 1,
            "step_state": {"mode": "regular_daily_briefing"},
        },
        "attempt_id": 42,
    }

    result = await execute_claimed_workflow_job(
        Settings(),
        WorkflowRuntime(settings=Settings()),
        job,
        worker_id="worker:test",
    )

    assert execute_mock.await_count == 1
    kwargs = execute_mock.await_args.kwargs
    assert kwargs["workflow_id"] == "regular_daily_briefing"
    assert kwargs["start_step_index"] == 1
    assert kwargs["initial_state"] == {"mode": "regular_daily_briefing"}
    assert result["completed_steps"] == 1
    assert result["state"]["writer_handoff"]["decision"] == "publish"
    assert result["state"]["writer_handoff"]["human_message"] == "ready"


@pytest.mark.asyncio
async def test_execute_claimed_workflow_job_runs_ai_review_handler(monkeypatch):
    review_mock = AsyncMock(
        return_value={
            "status": "stored",
            "review_id": str(uuid4()),
            "decision": "edit",
        }
    )
    update_mock = AsyncMock()
    monkeypatch.setattr("bcn.workflows.queue.run_briefing_ai_review", review_mock)
    monkeypatch.setattr("bcn.workflows.queue.update_workflow_job_progress", update_mock)

    briefing_id = uuid4()
    job = {
        "id": uuid4(),
        "job_type": JOB_TYPE_BRIEFING_AI_REVIEW,
        "payload": {
            "briefing_id": str(briefing_id),
            "source": "auto_distribution",
        },
        "attempt_id": 7,
    }

    result = await execute_claimed_workflow_job(
        Settings(),
        WorkflowRuntime(settings=Settings()),
        job,
        worker_id="worker:test",
    )

    review_mock.assert_awaited_once()
    assert review_mock.await_args.kwargs["briefing_id"] == briefing_id
    assert result["status"] == "stored"
    update_mock.assert_awaited_once()
