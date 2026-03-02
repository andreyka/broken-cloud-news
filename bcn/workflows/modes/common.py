"""Shared helpers for workflow modes."""

from __future__ import annotations

from collections.abc import Awaitable
from collections.abc import Callable
import logging
import re
from uuid import UUID

from bcn.workflows.runtime import require_runtime

logger = logging.getLogger(__name__)
_UUID_PATTERN = re.compile(
    r"\b[0-9a-fA-F]{8}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}\b"
)


def extract_briefing_id(text: str) -> UUID | None:
    """Extract a briefing UUID from writer/distributor message text."""
    match = _UUID_PATTERN.search(text or "")
    if not match:
        return None
    try:
        return UUID(match.group(0))
    except ValueError:
        return None


async def run_writer_distributor_handoff(
    *,
    mode: str,
    run_writer: Callable[[str], Awaitable[str]],
    run_distributor: Callable[[str], Awaitable[str]],
) -> tuple[str, str | None]:
    """Run writer->distributor handoff using shared skill-string logic."""
    writer_result = await run_writer(f"generate_briefing::{mode}")
    briefing_id = extract_briefing_id(writer_result)
    if not briefing_id:
        logger.warning(
            "Writer did not return a briefing id; skipping distribution. mode=%s writer_result=%s",
            mode,
            writer_result,
        )
        return writer_result, None

    distributor_result = await run_distributor(
        f"distribute_briefing::{briefing_id}::{mode}",
    )
    return writer_result, distributor_result


async def run_generation_and_distribution(mode: str) -> None:
    """Run one writer->distributor handoff cycle for the given workflow mode."""
    settings, sender = require_runtime()
    await run_writer_distributor_handoff(
        mode=mode,
        run_writer=lambda skill: sender(settings.writer_port, skill),
        run_distributor=lambda skill: sender(settings.distributor_port, skill),
    )
