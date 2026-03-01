from __future__ import annotations

from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from bcn.common.config import Settings


def _make_settings(**overrides) -> Settings:
    defaults = dict(
        database_url="postgresql://test:test@localhost:5432/test",
        llm_base_url="http://fake-llm:8000/v1",
        llm_model="test-model",
        comfyui_url="http://fake-comfy:8188",
        github_token="ghp_fake",
    )
    defaults.update(overrides)
    return Settings(**defaults)


class _FakeEventQueue:
    def __init__(self):
        self.events: list = []

    def enqueue_event(self, event):
        self.events.append(event)


def _fake_context(text: str = "collect_all"):
    from uuid import uuid4

    from a2a.server.agent_execution import RequestContext
    from a2a.types import Message
    from a2a.types import MessageSendParams
    from a2a.types import TextPart

    msg = Message(role="user", parts=[TextPart(text=text)], message_id=uuid4().hex)
    return RequestContext(request=MessageSendParams(message=msg))


@pytest.mark.asyncio
async def test_execute_reports_failed_sources_in_collect_all():
    from bcn.agents.collector.agent import CollectorExecutor

    settings = _make_settings()
    executor = CollectorExecutor(settings)

    with (
        patch.object(executor, "_collect_ghsa", new_callable=AsyncMock, return_value=1),
        patch.object(
            executor,
            "_collect_rss",
            new_callable=AsyncMock,
            side_effect=RuntimeError("rss failure"),
        ),
        patch.object(
            executor,
            "_collect_twitter",
            new_callable=AsyncMock,
            return_value=3,
        ),
        patch.object(executor, "_collect_reddit", new_callable=AsyncMock, return_value=4),
    ):
        eq = _FakeEventQueue()
        ctx = _fake_context("collect")
        await executor.execute(ctx, eq)

    assert any("All: GHSA=1, RSS=0, Twitter=3, Reddit=4" in str(e) for e in eq.events)
    assert any("failures: rss" in str(e).lower() for e in eq.events)
