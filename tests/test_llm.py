from __future__ import annotations

import json

import httpx
import pytest
import respx

from bcn.llm import LLMClient, build_prompt_versions


@pytest.fixture
def llm():
    return LLMClient(base_url="http://fake-llm:8000/v1", model="test-model", timeout=5)


def test_build_prompt_versions_has_expected_keys():
    versions = build_prompt_versions()
    assert "briefing_system" in versions
    assert "briefing_critic" in versions
    assert "sha256" in versions["briefing_system"]


def _chat_response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": content}}]},
    )


class TestChat:
    @respx.mock
    @pytest.mark.asyncio
    async def test_basic_chat(self, llm):
        respx.post("http://fake-llm:8000/v1/chat/completions").mock(
            return_value=_chat_response("hello")
        )
        result = await llm.chat("system", "user")
        assert result == "hello"

    @respx.mock
    @pytest.mark.asyncio
    async def test_retry_on_timeout(self, llm):
        route = respx.post("http://fake-llm:8000/v1/chat/completions")
        route.side_effect = [
            httpx.ConnectError("fail"),
            _chat_response("recovered"),
        ]
        result = await llm.chat("system", "user", retries=2)
        assert result == "recovered"
        assert route.call_count == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_exhausted_retries(self, llm):
        respx.post("http://fake-llm:8000/v1/chat/completions").mock(
            side_effect=httpx.ConnectError("fail")
        )
        with pytest.raises(httpx.ConnectError):
            await llm.chat("system", "user", retries=1)


class TestAnalyzeItem:
    @respx.mock
    @pytest.mark.asyncio
    async def test_valid_json(self, llm):
        payload = json.dumps({
            "summary": "Critical vuln in k8s",
            "relevance_score": 9,
            "tags": ["k8s", "cloud"],
            "image_prompt": "cyberpunk",
        })
        respx.post("http://fake-llm:8000/v1/chat/completions").mock(
            return_value=_chat_response(payload)
        )
        result = await llm.analyze_item("CVE-2026-1234", "details")
        assert result.relevance_score == 9
        assert result.summary == "Critical vuln in k8s"
        assert "k8s" in result.tags

    @respx.mock
    @pytest.mark.asyncio
    async def test_json_in_markdown_fences(self, llm):
        payload = '```json\n{"summary":"test","relevance_score":5,"tags":[],"image_prompt":"x"}\n```'
        respx.post("http://fake-llm:8000/v1/chat/completions").mock(
            return_value=_chat_response(payload)
        )
        result = await llm.analyze_item("title", "content")
        assert result.summary == "test"
        assert result.relevance_score == 5

    @respx.mock
    @pytest.mark.asyncio
    async def test_invalid_json_fallback(self, llm):
        respx.post("http://fake-llm:8000/v1/chat/completions").mock(
            return_value=_chat_response("This is not JSON at all")
        )
        result = await llm.analyze_item("title", "content")
        assert result.relevance_score == 5
        assert result.summary == "This is not JSON at all"

    @respx.mock
    @pytest.mark.asyncio
    async def test_score_clamped(self, llm):
        payload = json.dumps({"summary": "s", "relevance_score": 99, "tags": [], "image_prompt": "x"})
        respx.post("http://fake-llm:8000/v1/chat/completions").mock(
            return_value=_chat_response(payload)
        )
        result = await llm.analyze_item("title", "content")
        assert result.relevance_score == 10


class TestGenerateBriefing:
    @respx.mock
    @pytest.mark.asyncio
    async def test_generates_briefing(self, llm):
        respx.post("http://fake-llm:8000/v1/chat/completions").mock(
            return_value=_chat_response("## Cloud Chaos\nBig day.")
        )
        items = [
            {"title": "CVE-1", "url": "https://a.com", "summary": "bad",
             "relevance_score": 9, "source_type": "ghsa", "raw_data": None},
        ]
        result = await llm.generate_briefing(items)
        assert "Cloud Chaos" in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_includes_reference_links(self, llm):
        route = respx.post("http://fake-llm:8000/v1/chat/completions").mock(
            return_value=_chat_response("briefing text")
        )
        items = [
            {
                "title": "CVE-1", "url": "https://a.com", "summary": "bad",
                "relevance_score": 9, "source_type": "ghsa",
                "raw_data": {
                    "references": [
                        {"url": "https://blog.example.com/writeup"},
                        {"url": "https://nvd.nist.gov/vuln/detail/CVE-1"},  # should be skipped
                    ]
                },
            },
        ]
        await llm.generate_briefing(items)
        body = json.loads(route.calls[0].request.content)
        user_msg = body["messages"][1]["content"]
        assert "blog.example.com/writeup" in user_msg
        assert "nvd.nist.gov" not in user_msg

    @respx.mock
    @pytest.mark.asyncio
    async def test_includes_style_memory(self, llm):
        route = respx.post("http://fake-llm:8000/v1/chat/completions").mock(
            return_value=_chat_response("briefing text")
        )
        items = [
            {"title": "CVE-1", "url": "https://a.com", "summary": "bad",
             "relevance_score": 9, "source_type": "ghsa", "raw_data": None},
        ]
        history = [
            {
                "content_markdown": "![Daily Cover](https://img)\n\nOld opener\n\n**Old Theme**\nStuff\n\nOld closer",
            }
        ]
        await llm.generate_briefing(items, recent_briefings=history)
        assert route.call_count >= 1
        msgs = []
        for call in route.calls:
            body = json.loads(call.request.content)
            msgs.append(body["messages"][1]["content"])
        assert any("Recent briefing patterns to avoid repeating" in m for m in msgs)


class TestGenerateCoverPrompt:
    @respx.mock
    @pytest.mark.asyncio
    async def test_strips_backticks(self, llm):
        respx.post("http://fake-llm:8000/v1/chat/completions").mock(
            return_value=_chat_response("`cyberpunk server room`")
        )
        result = await llm.generate_cover_prompt("topics")
        assert "`" not in result
        assert "cyberpunk server room" in result
