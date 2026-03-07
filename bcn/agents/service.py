"""Service helpers for BCN agent invocations."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from uuid import UUID

from bcn.common.agent_client import AgentClient
from bcn.common.agent_client import build_default_agent_client
from bcn.common.config import Settings

CollectionSource = Literal["all", "ghsa", "rss", "twitter", "reddit"]


def _resolve_agent_client(
    settings: Settings,
    agent_client: AgentClient | None,
) -> AgentClient:
    """Return the provided client or the default client for these settings."""
    return agent_client if agent_client is not None else build_default_agent_client(settings)


def _resolve_markdown_input(
    *,
    latest: bool,
    file_path: str | None,
    text_input: str | None,
    latest_skill: str,
    markdown_skill_prefix: str,
) -> str:
    """Return the skill payload for latest-vs-explicit markdown commands."""
    if text_input:
        return f"{markdown_skill_prefix}{text_input}"
    if file_path:
        body = Path(file_path).read_text(encoding="utf-8")
        return f"{markdown_skill_prefix}{body}"
    return latest_skill if latest or (not file_path and not text_input) else ""


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
    client = _resolve_agent_client(settings, agent_client)
    skill = _resolve_markdown_input(
        latest=latest,
        file_path=file_path,
        text_input=text_input,
        latest_skill="critique_latest",
        markdown_skill_prefix="critique_markdown::",
    )
    if skill == "critique_latest":
        return await client.critique_latest()
    return await client.call_critic(skill)


async def verify_briefing(
    settings: Settings,
    *,
    latest: bool = False,
    file_path: str | None = None,
    text_input: str | None = None,
    agent_client: AgentClient | None = None,
) -> str:
    """Verify the latest or explicitly supplied markdown."""
    client = _resolve_agent_client(settings, agent_client)
    skill = _resolve_markdown_input(
        latest=latest,
        file_path=file_path,
        text_input=text_input,
        latest_skill="verify_latest",
        markdown_skill_prefix="verify_markdown::",
    )
    if skill == "verify_latest":
        return await client.verify_latest()
    return await client.call_verifier(skill)


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
