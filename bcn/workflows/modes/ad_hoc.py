"""Ad-hoc briefing workflow mode."""

from __future__ import annotations

from bcn.workflows.modes.common import run_generation_and_distribution

MODE = "ad_hoc"


async def run() -> None:
    """Execute one ad-hoc briefing publication cycle."""
    await run_generation_and_distribution(mode=MODE)

