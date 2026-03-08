"""Shared helpers for workflow modes."""

from __future__ import annotations

from collections.abc import Awaitable
from collections.abc import Callable
import logging
from uuid import UUID

from bcn.contracts.workflow import WriterHandoff
from bcn.contracts.workflow import WriterHandoffResult
from bcn.contracts.workflow import extract_briefing_id
from bcn.contracts.workflow import parse_writer_handoff_payload
from bcn.contracts.workflow import render_writer_handoff_message
from bcn.contracts.workflow import render_writer_handoff_payload
from bcn.workflows.distribution import execute_distribution
from bcn.workflows.runtime import WorkflowRuntime

logger = logging.getLogger(__name__)

async def run_writer_distributor_handoff(
    *,
    mode: str,
    run_generation: Callable[[str], Awaitable[WriterHandoffResult | str]],
    run_distribution: Callable[[str, UUID], Awaitable[str]],
) -> tuple[str, str | None]:
    """Run writer->distributor handoff from explicit writer payload."""
    writer_result = await run_generation(mode)
    if isinstance(writer_result, WriterHandoffResult):
        handoff = writer_result.handoff
        rendered_writer_result = render_writer_handoff_message(
            handoff,
            human_message=writer_result.human_message,
        )
    else:
        rendered_writer_result = str(writer_result)
        handoff = parse_writer_handoff_payload(rendered_writer_result)
    if not handoff:
        logger.warning(
            "Writer did not return structured handoff; skipping distribution. mode=%s writer_result=%s",
            mode,
            rendered_writer_result,
        )
        return rendered_writer_result, None

    if handoff.decision != "publish":
        logger.info(
            "Writer decision=%s; skipping distribution. mode=%s",
            handoff.decision,
            handoff.mode,
        )
        return rendered_writer_result, None

    if handoff.briefing_id is None:
        logger.warning(
            "Writer publish decision missing briefing_id; skipping distribution. mode=%s writer_result=%s",
            handoff.mode,
            rendered_writer_result,
        )
        return rendered_writer_result, None

    dispatch_mode = mode
    if handoff.mode and handoff.mode != mode:
        logger.warning(
            "Writer handoff mode mismatch (handoff=%s, expected=%s); using expected mode.",
            handoff.mode,
            mode,
        )
    distributor_result = await run_distribution(dispatch_mode, handoff.briefing_id)
    return rendered_writer_result, distributor_result


async def run_generation_and_distribution(
    *,
    runtime: WorkflowRuntime,
    mode: str,
) -> None:
    """Run one writer->distributor handoff cycle for the given workflow mode."""
    from bcn.workflows.generation import execute_generation_result

    await run_writer_distributor_handoff(
        mode=mode,
        run_generation=lambda workflow_mode: execute_generation_result(
            runtime.settings,
            mode=workflow_mode,
            source="scheduler",
            manage_pool=False,
        ),
        run_distribution=lambda dispatch_mode, briefing_id: execute_distribution(
            runtime.settings,
            mode=dispatch_mode,
            briefing_id=briefing_id,
            manage_pool=False,
        ),
    )
