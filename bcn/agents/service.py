"""Service helpers for BCN agent invocations."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from uuid import UUID

from bcn.common.agent_client import AgentClient
from bcn.common.config import Settings

CollectionSource = Literal["all", "ghsa", "rss", "twitter", "reddit"]

def _resolve_markdown_text(
    *,
    file_path: str | None,
    text_input: str | None,
) -> str | None:
    """Return explicit markdown text from CLI input, if any."""
    if text_input:
        return text_input
    if file_path:
        return Path(file_path).read_text(encoding="utf-8")
    return None


async def collect_news(
    settings: Settings,
    *,
    source: CollectionSource = "all",
    agent_client: AgentClient | None = None,
) -> str:
    """Run one collector source or the full collector fan-out."""
    del agent_client

    from bcn.workflows.collection import execute_collection

    return await execute_collection(
        settings,
        source=source,
        origin="cli",
        manage_pool=True,
    )


async def analyze_items(
    settings: Settings,
    *,
    agent_client: AgentClient | None = None,
) -> str:
    """Backward-compatible wrapper for control-plane analysis."""
    del agent_client

    from bcn.workflows.analysis import execute_analysis

    return await execute_analysis(
        settings,
        source="cli",
        manage_pool=True,
    )


async def generate_briefing(
    settings: Settings,
    *,
    mode: str,
    agent_client: AgentClient | None = None,
) -> str:
    """Backward-compatible wrapper for control-plane generation."""
    from bcn.workflows.generation import execute_generation

    return await execute_generation(
        settings,
        mode=mode,
        source="cli",
        manage_pool=True,
    )


async def critique_briefing(
    settings: Settings,
    *,
    latest: bool = False,
    file_path: str | None = None,
    text_input: str | None = None,
    agent_client: AgentClient | None = None,
) -> str:
    """Critique the latest or explicitly supplied markdown."""
    del agent_client

    from bcn.workflows.review import execute_critique

    markdown = _resolve_markdown_text(
        file_path=file_path,
        text_input=text_input,
    )
    return await execute_critique(
        settings,
        latest=latest or markdown is None,
        markdown=markdown,
        source="cli",
        manage_pool=True,
    )


async def verify_briefing(
    settings: Settings,
    *,
    latest: bool = False,
    file_path: str | None = None,
    text_input: str | None = None,
    agent_client: AgentClient | None = None,
) -> str:
    """Verify the latest or explicitly supplied markdown."""
    del agent_client

    from bcn.workflows.review import execute_verification

    markdown = _resolve_markdown_text(
        file_path=file_path,
        text_input=text_input,
    )
    return await execute_verification(
        settings,
        latest=latest or markdown is None,
        markdown=markdown,
        source="cli",
        manage_pool=True,
    )


async def distribute_briefing(
    settings: Settings,
    *,
    mode: str,
    briefing_id: UUID | None = None,
    agent_client: AgentClient | None = None,
) -> str:
    """Backward-compatible wrapper for control-plane distribution."""
    from bcn.workflows.distribution import execute_distribution

    return await execute_distribution(
        settings,
        mode=mode,
        briefing_id=briefing_id,
        manage_pool=True,
    )
