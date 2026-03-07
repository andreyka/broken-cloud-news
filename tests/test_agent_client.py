from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from bcn.common.agent_client import A2AAgentClient
from bcn.common.agent_client import DirectAgentClient
from bcn.common.agent_client import build_default_agent_client
from bcn.common.agent_client import build_port_sender_agent_client
from bcn.common.config import Settings


def _make_settings(**overrides) -> Settings:
    defaults = {
        "database_url": "postgresql://test:test@localhost:5432/test",
        "llm_base_url": "http://fake-llm:8000/v1",
        "llm_model": "test-model",
        "comfyui_url": "http://fake-comfy:8188",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def test_build_default_agent_client_uses_direct_transport_without_overrides():
    settings = _make_settings()

    client = build_default_agent_client(settings)

    assert isinstance(client, DirectAgentClient)


def test_build_default_agent_client_uses_a2a_transport_with_endpoint_override():
    settings = _make_settings(writer_agent_url="https://writer.example.com/a2a")

    client = build_default_agent_client(settings)

    assert isinstance(client, A2AAgentClient)


@pytest.mark.asyncio
async def test_port_sender_agent_client_routes_to_local_agent_ports():
    settings = _make_settings(collector_port=9101)
    sender = AsyncMock(return_value="GHSA: collected 1 items")
    client = build_port_sender_agent_client(settings, sender=sender)

    result = await client.collect_ghsa()

    assert result == "GHSA: collected 1 items"
    sender.assert_awaited_once_with(9101, "collect_ghsa")


@pytest.mark.asyncio
async def test_a2a_agent_client_uses_resolved_endpoint_urls():
    settings = _make_settings(writer_agent_url="https://writer.example.com/a2a/")
    sender = AsyncMock(
        return_value="writer_handoff::{}"
    )
    client = A2AAgentClient(settings=settings, timeout_seconds=45, sender=sender)

    result = await client.generate_briefing("regular_daily_briefing")

    assert result == "writer_handoff::{}"
    sender.assert_awaited_once_with(
        "https://writer.example.com/a2a",
        "generate_briefing::regular_daily_briefing",
        timeout_seconds=45,
    )


@pytest.mark.asyncio
async def test_direct_agent_client_uses_runner_and_executor_lookup():
    settings = _make_settings()
    runner = AsyncMock(return_value="Analyzed 3/3 items")
    client = DirectAgentClient(settings=settings, runner=runner)

    result = await client.analyze_new_items()

    assert result == "Analyzed 3/3 items"
    kwargs = runner.await_args.kwargs
    assert kwargs["skill"] == "analyze_new_items"
    assert kwargs["executor_cls"].__name__ == "AnalystExecutor"
