"""Shared runtime dependencies for workflow execution."""

from __future__ import annotations

from collections.abc import Awaitable
from collections.abc import Callable

from bcn.common.agent_client import AgentClient
from bcn.common.agent_client import build_port_sender_agent_client
from bcn.common.config import Settings

AgentSender = Callable[[int, str], Awaitable[str]]

_settings: Settings | None = None
_agent_client: AgentClient | None = None


def configure_runtime(
    settings: Settings,
    *,
    agent_client: AgentClient | None = None,
    sender: AgentSender | None = None,
) -> None:
    """Configure runtime dependencies for workflow modules."""
    global _settings
    global _agent_client
    _settings = settings
    if agent_client is not None:
        _agent_client = agent_client
        return
    if sender is None:
        raise RuntimeError(
            "Workflow runtime requires either an agent client or sender adapter."
        )
    _agent_client = build_port_sender_agent_client(settings, sender=sender)


def require_runtime() -> tuple[Settings, AgentClient]:
    """Return configured runtime dependencies or raise a clear error."""
    if _settings is None or _agent_client is None:
        raise RuntimeError("Workflow runtime not configured; call configure_runtime.")
    return _settings, _agent_client
