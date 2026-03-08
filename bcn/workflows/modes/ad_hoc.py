"""Ad-hoc briefing workflow mode."""

from __future__ import annotations

from bcn.workflows.modes.common import run_generation_and_distribution
from bcn.workflows.runtime import WorkflowRuntime

MODE = "ad_hoc"


async def run(runtime: WorkflowRuntime) -> None:
    """Execute one ad-hoc briefing publication cycle."""
    await run_generation_and_distribution(runtime=runtime, mode=MODE)
