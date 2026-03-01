"""Shared runtime dependencies for workflow execution."""

from __future__ import annotations

from collections.abc import Awaitable
from collections.abc import Callable

from bcn.common.config import Settings

AgentSender = Callable[[int, str], Awaitable[str]]

_settings: Settings | None = None
_send_to_agent: AgentSender | None = None


def configure_runtime(settings: Settings, sender: AgentSender) -> None:
    """Configure runtime dependencies for workflow modules."""
    global _settings
    global _send_to_agent
    _settings = settings
    _send_to_agent = sender


def require_runtime() -> tuple[Settings, AgentSender]:
    """Return configured runtime dependencies or raise a clear error."""
    if _settings is None or _send_to_agent is None:
        raise RuntimeError("Workflow runtime not configured; call configure_runtime.")
    return _settings, _send_to_agent

