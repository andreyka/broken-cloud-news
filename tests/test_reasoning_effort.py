"""Regression tests for the per-role reasoning_effort passthrough."""

import json

import httpx
import pytest
import respx

from bcn.common.llm import LLMClient


def _client() -> LLMClient:
    return LLMClient(
        base_url="https://api.openai.com/v1",
        model="gpt-5.6",
        timeout=5,
        reasoning_effort="xhigh",
    )


def _ok_response() -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})


def _dummy_tool(url: str) -> str:
    return url


@respx.mock
@pytest.mark.asyncio
async def test_reasoning_effort_sent_without_tools():
    route = respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=_ok_response()
    )
    await _client().chat_for_role(
        role="writer", system_prompt="s", user_content="u", retries=1
    )
    payload = json.loads(route.calls.last.request.content)
    assert payload["reasoning_effort"] == "xhigh"


@respx.mock
@pytest.mark.asyncio
async def test_tools_force_reasoning_effort_none_on_gpt():
    """gpt-5.6+ requires an explicit reasoning_effort='none' with tools."""
    route = respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=_ok_response()
    )
    await _client().chat_for_role(
        role="writer",
        system_prompt="s",
        user_content="u",
        retries=1,
        tools=[_dummy_tool],
    )
    payload = json.loads(route.calls.last.request.content)
    assert "tools" in payload
    assert payload["reasoning_effort"] == "none"


@respx.mock
@pytest.mark.asyncio
async def test_tools_omit_reasoning_effort_on_pre_56_gpt():
    """gpt-5-mini rejects reasoning_effort='none'; the param must be absent."""
    route = respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=_ok_response()
    )
    client = LLMClient(
        base_url="https://api.openai.com/v1",
        model="gpt-5-mini",
        timeout=5,
    )
    await client.chat_for_role(
        role="analyst",
        system_prompt="s",
        user_content="u",
        retries=1,
        tools=[_dummy_tool],
    )
    payload = json.loads(route.calls.last.request.content)
    assert "tools" in payload
    assert "reasoning_effort" not in payload


@respx.mock
@pytest.mark.asyncio
async def test_tools_strip_reasoning_effort_on_other_backends():
    route = respx.post("https://bridge.local/v1/chat/completions").mock(
        return_value=_ok_response()
    )
    client = LLMClient(
        base_url="https://bridge.local/v1",
        model="unsloth/Qwen3.8-27B-NVFP4",
        timeout=5,
        reasoning_effort="xhigh",
    )
    await client.chat_for_role(
        role="writer",
        system_prompt="s",
        user_content="u",
        retries=1,
        tools=[_dummy_tool],
    )
    payload = json.loads(route.calls.last.request.content)
    assert "tools" in payload
    assert "reasoning_effort" not in payload


@respx.mock
@pytest.mark.asyncio
async def test_reasoning_effort_not_sent_for_non_reasoning_models():
    route = respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=_ok_response()
    )
    client = LLMClient(
        base_url="https://api.openai.com/v1",
        model="unsloth/Qwen3.8-27B-NVFP4",
        timeout=5,
        reasoning_effort="xhigh",
    )
    await client.chat_for_role(
        role="writer", system_prompt="s", user_content="u", retries=1
    )
    payload = json.loads(route.calls.last.request.content)
    assert "reasoning_effort" not in payload
