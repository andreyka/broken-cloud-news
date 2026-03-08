"""Shared runtime dependencies for workflow execution."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Awaitable
from collections.abc import Callable

from bcn.common.agent_client import AgentClient
from bcn.common.agent_client import build_port_sender_agent_client
from bcn.common.config import Settings

AgentSender = Callable[[int, str], Awaitable[str]]


@dataclass(frozen=True)
class WorkflowRuntime:
    """Explicit runtime dependencies shared across scheduler workflow jobs."""

    settings: Settings
    agent_client: AgentClient


def build_workflow_runtime(
    settings: Settings,
    *,
    agent_client: AgentClient | None = None,
    sender: AgentSender | None = None,
) -> WorkflowRuntime:
    """Build an explicit workflow runtime for scheduler and mode execution."""
    if agent_client is not None:
        return WorkflowRuntime(settings=settings, agent_client=agent_client)
    if sender is None:
        raise RuntimeError(
            "Workflow runtime requires either an agent client or sender adapter."
        )
    return WorkflowRuntime(
        settings=settings,
        agent_client=build_port_sender_agent_client(settings, sender=sender),
    )
