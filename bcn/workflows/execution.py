"""Declarative workflow-step execution for the BCN control plane."""

from __future__ import annotations

from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any
from uuid import UUID

from bcn.contracts.workflow import WriterHandoff
from bcn.contracts.workflow import WriterHandoffResult
from bcn.workflows.automation import execute_scheduled_analysis
from bcn.workflows.automation import execute_scheduled_ai_review_backfill
from bcn.workflows.automation import execute_scheduled_collection
from bcn.workflows.automation import execute_shadow_regular_briefing
from bcn.workflows.catalog import WorkflowStepDefinition
from bcn.workflows.distribution import execute_distribution
from bcn.workflows.generation import execute_generation_result
from bcn.workflows.runtime import WorkflowRuntime

StepState = dict[str, Any]
StepExecutor = Callable[[WorkflowRuntime, dict[str, Any], StepState], Awaitable[StepState]]
StepProgressCallback = Callable[[int, WorkflowStepDefinition, StepState], Awaitable[None]]


def serialize_step_state(state: StepState) -> dict[str, Any]:
    """Return a JSON-safe workflow step state snapshot."""
    serialized = dict(state or {})
    writer_handoff = serialized.get("writer_handoff")
    if isinstance(writer_handoff, WriterHandoffResult):
        handoff = writer_handoff.handoff
        serialized["writer_handoff"] = {
            "mode": handoff.mode,
            "decision": handoff.decision,
            "briefing_id": str(handoff.briefing_id) if handoff.briefing_id else None,
            "item_count": handoff.item_count,
            "human_message": writer_handoff.human_message,
        }
    return serialized


def deserialize_step_state(state: StepState | None) -> StepState:
    """Restore typed workflow state from a JSON-safe checkpoint."""
    restored = dict(state or {})
    writer_handoff = restored.get("writer_handoff")
    if isinstance(writer_handoff, dict):
        briefing_id = writer_handoff.get("briefing_id")
        parsed_briefing_id = None
        if briefing_id:
            try:
                parsed_briefing_id = UUID(str(briefing_id))
            except (TypeError, ValueError):
                parsed_briefing_id = None
        restored["writer_handoff"] = WriterHandoffResult(
            handoff=WriterHandoff(
                mode=str(writer_handoff.get("mode") or ""),
                decision=str(writer_handoff.get("decision") or ""),
                briefing_id=parsed_briefing_id,
                item_count=writer_handoff.get("item_count"),
            ),
            human_message=str(writer_handoff.get("human_message") or ""),
        )
    return restored


async def _execute_collect_step(
    runtime: WorkflowRuntime,
    args: dict[str, Any],
    state: StepState,
) -> StepState:
    source = str(args.get("source") or "").strip().lower()
    if not source:
        raise ValueError("collector.collect step requires a source.")
    await execute_scheduled_collection(runtime, source=source)
    return state


async def _execute_analyze_pending_step(
    runtime: WorkflowRuntime,
    args: dict[str, Any],
    state: StepState,
) -> StepState:
    del args
    await execute_scheduled_analysis(runtime)
    return state


async def _execute_shadow_regular_briefing_step(
    runtime: WorkflowRuntime,
    args: dict[str, Any],
    state: StepState,
) -> StepState:
    del args
    await execute_shadow_regular_briefing(runtime)
    return state


async def _execute_backfill_ai_reviews_step(
    runtime: WorkflowRuntime,
    args: dict[str, Any],
    state: StepState,
) -> StepState:
    del args
    await execute_scheduled_ai_review_backfill(runtime)
    return state


async def _execute_generation_step(
    runtime: WorkflowRuntime,
    args: dict[str, Any],
    state: StepState,
) -> StepState:
    del state
    mode = str(args.get("mode") or "").strip()
    if not mode:
        raise ValueError("workflow.generation_and_distribution step requires a mode.")
    writer_result = await execute_generation_result(
        runtime.settings,
        mode=mode,
        source="scheduler",
        manage_pool=False,
    )
    return {
        "mode": mode,
        "writer_handoff": writer_result,
    }


async def _execute_distribution_step(
    runtime: WorkflowRuntime,
    args: dict[str, Any],
    state: StepState,
) -> StepState:
    mode = str(args.get("mode") or state.get("mode") or "").strip()
    if not mode:
        raise ValueError("distributor.deliver step requires a mode.")
    writer_handoff = state.get("writer_handoff")
    if not isinstance(writer_handoff, WriterHandoffResult):
        raise ValueError("distributor.deliver step requires a prior writer handoff.")
    handoff = writer_handoff.handoff
    if handoff.decision != "publish" or handoff.briefing_id is None:
        return state
    await execute_distribution(
        runtime.settings,
        mode=mode,
        briefing_id=handoff.briefing_id,
        manage_pool=False,
    )
    return state


_STEP_EXECUTORS: dict[tuple[str, str], StepExecutor] = {
    ("collector", "collect"): _execute_collect_step,
    ("analyst", "analyze_pending"): _execute_analyze_pending_step,
    ("workflow", "shadow_regular_briefing"): _execute_shadow_regular_briefing_step,
    ("workflow", "backfill_ai_reviews"): _execute_backfill_ai_reviews_step,
    ("writer", "generate_release_candidate"): _execute_generation_step,
    ("distributor", "deliver"): _execute_distribution_step,
}


async def execute_workflow_steps(
    runtime: WorkflowRuntime,
    *,
    workflow_id: str,
    steps: tuple[WorkflowStepDefinition, ...],
    initial_state: StepState | None = None,
    start_step_index: int = 0,
    on_step_complete: StepProgressCallback | None = None,
) -> StepState:
    """Execute one scheduled workflow from its declared steps."""
    state: StepState = deserialize_step_state(initial_state)
    for step_index, step in enumerate(steps[start_step_index:], start=start_step_index):
        key = (str(step.component or "").strip().lower(), str(step.operation or "").strip())
        executor = _STEP_EXECUTORS.get(key)
        if executor is None:
            raise ValueError(
                f"Workflow {workflow_id} references unsupported step "
                f"{step.component}.{step.operation}"
            )
        state = await executor(runtime, dict(step.args or {}), state)
        if on_step_complete is not None:
            await on_step_complete(step_index, step, serialize_step_state(state))
    return state


__all__ = [
    "deserialize_step_state",
    "execute_workflow_steps",
    "serialize_step_state",
]
