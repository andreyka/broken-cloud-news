"""Distribution channel implementations (Telegram, Email, Slack, Discord)."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Distributor(Protocol):
    """Interface that all distribution channels must satisfy."""

    last_result: dict[str, Any]

    async def send(self, briefing: Any) -> bool:
        """Send a briefing to the channel. Return True on success."""
        ...

    async def close(self) -> None:
        """Release resources held by the channel."""
        ...
