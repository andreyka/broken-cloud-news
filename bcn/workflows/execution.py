"""Declarative workflow-step execution for the BCN control plane."""

from __future__ import annotations

from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any

from bcn.contracts.workflow import WriterHandoffResult
from bcn.workflows.automation import execute_scheduled_analysis
from bcn.workflows.automation import execute_scheduled_collection
from bcn.workflows.automation import execute_shadow_regular_briefing
from bcn.workflows.distribution import execute_distribution
from bcn.workflows.generation import execute_generation_result
from bcn.workflows.runtime import WorkflowRuntime

StepState = dict[str, Any]
StepExecutor = Callable[[WorkflowRuntime, dict[str, Any], StepState], Awaitable[StepState]]


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
    ("writer", "generate_release_candidate"): _execute_generation_step,
    ("distributor", "deliver"): _execute_distribution_step,
}


async def execute_workflow_steps(
    runtime: WorkflowRuntime,
    *,
    workflow_id: str,
    steps: tuple[Any, ...],
) -> StepState:
    """Execute one scheduled workflow from its declared steps."""
    state: StepState = {}
    for step in steps:
        key = (str(step.component or "").strip().lower(), str(step.operation or "").strip())
        executor = _STEP_EXECUTORS.get(key)
        if executor is None:
            raise ValueError(
                f"Workflow {workflow_id} references unsupported step "
                f"{step.component}.{step.operation}"
            )
        state = await executor(runtime, dict(step.args or {}), state)
    return state


__all__ = [
    "execute_workflow_steps",
]
