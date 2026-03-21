from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from bcn.common.config import Settings
from bcn.contracts.workflow import WriterHandoff
from bcn.contracts.workflow import WriterHandoffResult
from bcn.workflows.catalog import get_scheduled_workflow_definition
from bcn.workflows.queue import execute_claimed_workflow_job
from bcn.workflows.queue import enqueue_scheduled_workflow_job
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
