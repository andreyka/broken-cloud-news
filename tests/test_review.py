from __future__ import annotations

import json
from unittest.mock import AsyncMock
from unittest.mock import patch
from uuid import uuid4

import pytest

from bcn.agents.critic.service import CritiqueRequest
from bcn.agents.critic.service import render_critique_request_payload
from bcn.agents.verifier.service import VerificationRequest
from bcn.agents.verifier.service import render_verification_request_payload
from bcn.common.config import Settings
from bcn.workflows.review import execute_critique
from bcn.workflows.review import execute_verification


def _make_settings(**overrides) -> Settings:
    defaults = {
        "database_url": "postgresql://test:test@localhost:5432/test",
        "llm_base_url": "http://fake-llm:8000/v1",
        "llm_model": "test-model",
        "comfyui_url": "http://fake-comfy:8188",
    }
    defaults.update(overrides)
    return Settings(**defaults)


class _FakeEventQueue:
    def __init__(self):
        self.events: list = []

    def enqueue_event(self, event):
        self.events.append(event)


def _fake_context(text: str):
    from a2a.server.agent_execution import RequestContext
    from a2a.types import Message
    from a2a.types import MessageSendParams
    from a2a.types import TextPart

    msg = Message(role="user", parts=[TextPart(text=text)], message_id=uuid4().hex)
    return RequestContext(request=MessageSendParams(message=msg))


@pytest.mark.asyncio
async def test_execute_critique_loads_latest_briefing_from_control_plane():
    settings = _make_settings()
    briefing_id = uuid4()
    service = AsyncMock()
    service.evaluate.return_value = {
        "source": f"briefing:{briefing_id}",
        "gate_passed": True,
        "critic_passed": True,
        "critic_score": 91,
        "critic_dimension_scores": {"actionability": 90},
        "threshold_passed": True,
        "thresholds": {"min_score": 80},
        "gate_issues": [],
        "critic_issues": [],
        "recommendations": [],
    }

    with (
        patch("bcn.workflows.review.get_pool", new_callable=AsyncMock),
        patch(
            "bcn.workflows.review.get_latest_any_briefing",
            new_callable=AsyncMock,
            return_value={
                "id": briefing_id,
                "content_markdown": "**Draft**",
                "item_ids": [uuid4()],
            },
        ),
        patch(
            "bcn.workflows.review.get_items_by_ids",
            new_callable=AsyncMock,
            return_value=[{"url": "https://example.com/one", "title": "One"}],
        ),
    ):
        result = await execute_critique(
            settings,
            latest=True,
            critic_service=service,
            source="cli",
            manage_pool=False,
        )

    service.evaluate.assert_awaited_once()
    request = service.evaluate.await_args.args[0]
    assert request.draft_markdown == "**Draft**"
    assert request.source == f"briefing:{briefing_id}"
    assert list(request.items) == [{"url": "https://example.com/one", "title": "One"}]
    payload = json.loads(result)
    assert payload["critic_score"] == 91


@pytest.mark.asyncio
async def test_execute_verification_with_explicit_markdown_skips_latest_lookup():
    settings = _make_settings()
    service = AsyncMock()
    service.evaluate.return_value = {
        "source": "cli",
        "verifier_passed": True,
        "verifier_score": 95,
        "issues": [],
        "recommendations": [],
        "dead_urls": [],
        "top_story_ok": True,
    }

    with (
        patch("bcn.workflows.review.get_pool", new_callable=AsyncMock),
        patch(
            "bcn.workflows.review.get_latest_any_briefing",
            new_callable=AsyncMock,
        ) as mock_latest,
    ):
        result = await execute_verification(
            settings,
            markdown="**Explicit**",
            verifier_service=service,
            source="cli",
            manage_pool=False,
        )

    mock_latest.assert_not_awaited()
    service.evaluate.assert_awaited_once()
    request = service.evaluate.await_args.args[0]
    assert request.draft_markdown == "**Explicit**"
    assert request.source == "cli"
    payload = json.loads(result)
    assert payload["verifier_score"] == 95


@pytest.mark.asyncio
async def test_critic_executor_requires_explicit_payload():
    from bcn.agents.critic.agent import CriticExecutor

    settings = _make_settings()
    executor = CriticExecutor(settings)

    try:
        eq = _FakeEventQueue()
        await executor.execute(_fake_context("critique_latest"), eq)
    finally:
        await executor.close()

    assert any("explicit briefing payload" in str(event).lower() for event in eq.events)


@pytest.mark.asyncio
async def test_critic_executor_evaluates_explicit_payload():
    from bcn.agents.critic.agent import CriticExecutor

    settings = _make_settings()
    executor = CriticExecutor(settings)
    payload = render_critique_request_payload(
        CritiqueRequest(
            draft_markdown="**Draft**",
            items=({"url": "https://example.com/one", "title": "One"},),
            source="briefing:test",
        )
    )

    try:
        with patch.object(
            executor._service,
            "evaluate",
            new_callable=AsyncMock,
            return_value={"source": "briefing:test", "critic_score": 88},
        ) as mock_evaluate:
            eq = _FakeEventQueue()
            await executor.execute(_fake_context(payload), eq)
    finally:
        await executor.close()

    mock_evaluate.assert_awaited_once()
    assert any('"critic_score": 88' in str(event) for event in eq.events)


@pytest.mark.asyncio
async def test_verifier_executor_evaluates_explicit_payload():
    from bcn.agents.verifier.agent import VerifierExecutor

    settings = _make_settings()
    executor = VerifierExecutor(settings)
    payload = render_verification_request_payload(
        VerificationRequest(
            draft_markdown="**Draft**",
            items=({"url": "https://example.com/one", "title": "One"},),
            source="briefing:test",
        )
    )

    try:
        with patch.object(
            executor._service,
            "evaluate",
            new_callable=AsyncMock,
            return_value={"source": "briefing:test", "verifier_score": 92},
        ) as mock_evaluate:
            eq = _FakeEventQueue()
            await executor.execute(_fake_context(payload), eq)
    finally:
        await executor.close()

    mock_evaluate.assert_awaited_once()
    assert any('"verifier_score": 92' in str(event) for event in eq.events)
