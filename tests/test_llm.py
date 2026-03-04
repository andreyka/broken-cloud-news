from __future__ import annotations

import json

import httpx
import pytest
import respx

from bcn.agents.analyst.llm import AnalystLLM
from bcn.agents.writer.llm import WriterLLM
from bcn.common.config import Settings
from bcn.common.llm import LLMClient


@pytest.fixture
def llm():
    return LLMClient(base_url="http://fake-llm:8000/v1", model="test-model", timeout=5)


def test_build_prompt_versions_has_expected_keys(llm):
    # In earlier versions briefing_critic was in WriterLLM.prompt_versions, but let's just assert writer prompt exists
    versions = WriterLLM(llm).prompt_versions()
    assert "briefing_system" in versions
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
        payload = json.dumps(
            {
                "summary": "Critical vuln in k8s",
                "relevance_score": 9,
                "tags": ["k8s", "cloud"],
                "image_prompt": "cyberpunk",
            }
        )
        respx.post("http://fake-llm:8000/v1/chat/completions").mock(
            return_value=_chat_response(payload)
        )
        result = await AnalystLLM(llm).analyze_item("CVE-2026-1234", "details", "url")
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
        result = await AnalystLLM(llm).analyze_item("title", "content", "url")
        assert result.summary == "test"
        assert result.relevance_score == 5

    @respx.mock
    @pytest.mark.asyncio
    async def test_invalid_json_fallback(self, llm):
        respx.post("http://fake-llm:8000/v1/chat/completions").mock(
            return_value=_chat_response("This is not JSON at all")
        )
        result = await AnalystLLM(llm).analyze_item("title", "content", "url")
        assert result.relevance_score == 5
        assert result.summary == "This is not JSON at all"

    @respx.mock
    @pytest.mark.asyncio
    async def test_score_clamped(self, llm):
        payload = json.dumps(
            {"summary": "s", "relevance_score": 99, "tags": [], "image_prompt": "x"}
        )
        respx.post("http://fake-llm:8000/v1/chat/completions").mock(
            return_value=_chat_response(payload)
        )
        result = await AnalystLLM(llm).analyze_item("title", "content", "url")
        assert result.relevance_score == 10


class TestGenerateBriefing:
    @respx.mock
    @pytest.mark.asyncio
    async def test_generates_briefing(self, llm):
        respx.post("http://fake-llm:8000/v1/chat/completions").mock(
            return_value=_chat_response("## Cloud Chaos\nBig day.")
        )
        items = [
            {
                "title": "CVE-1",
                "url": "https://a.com",
                "summary": "bad",
                "relevance_score": 9,
                "source_type": "ghsa",
                "raw_data": None,
            },
        ]
        result = await WriterLLM(llm).generate_briefing(items)
        assert "Cloud Chaos" in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_includes_reference_links(self, llm):
        route = respx.post("http://fake-llm:8000/v1/chat/completions").mock(
            return_value=_chat_response("briefing text")
        )
        items = [
            {
                "title": "CVE-1",
                "url": "https://a.com",
                "summary": "bad",
                "relevance_score": 9,
                "source_type": "ghsa",
                "raw_data": {
                    "references": [
                        {"url": "https://blog.example.com/writeup"},
                        {
                            "url": "https://nvd.nist.gov/vuln/detail/CVE-1"
                        },  # should be skipped
                    ]
                },
            },
        ]
        await WriterLLM(llm).generate_briefing(items)
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
            {
                "title": "CVE-1",
                "url": "https://a.com",
                "summary": "bad",
                "relevance_score": 9,
                "source_type": "ghsa",
                "raw_data": None,
            },
        ]
        history = [
            {
                "content_markdown": "![Daily Cover](https://img)\n\nOld opener\n\n**Old Theme**\nStuff\n\nOld closer",
            }
        ]
        await WriterLLM(llm).generate_briefing(
            items, recent_briefings=history, mode="standard"
        )
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
        result = await WriterLLM(llm).generate_cover_prompt("topics")
        assert "`" not in result
        assert "cyberpunk server room" in result


class TestCoverImageSupport:
    def test_supports_gemini_image_model(self):
        client = LLMClient(
            base_url="https://aiplatform.googleapis.com/v1",
            model="gemini-3.1-pro-preview",
            timeout=5,
            provider="vertexai",
            role_overrides={
                "cover": {
                    "provider": "vertexai",
                    "base_url": "https://aiplatform.googleapis.com/v1",
                    "model": "gemini-3-pro-image-preview",
                }
            },
        )
        assert WriterLLM(client).supports_cover_image_generation() is True

    def test_supports_nanobanana_cover_model(self):
        client = LLMClient(
            base_url="https://aiplatform.googleapis.com/v1",
            model="gemini-3.1-pro-preview",
            timeout=5,
            provider="vertexai",
            role_overrides={
                "cover": {
                    "provider": "vertexai",
                    "base_url": "https://aiplatform.googleapis.com/v1",
                    "model": "nanobanana-pro2",
                }
            },
        )
        assert WriterLLM(client).supports_cover_image_generation() is True

    def test_rejects_text_only_cover_model(self):
        client = LLMClient(
            base_url="https://aiplatform.googleapis.com/v1",
            model="gemini-3.1-pro-preview",
            timeout=5,
            provider="vertexai",
            role_overrides={
                "cover": {
                    "provider": "vertexai",
                    "base_url": "https://aiplatform.googleapis.com/v1",
                    "model": "gemini-3.1-pro-preview",
                }
            },
        )
        assert WriterLLM(client).supports_cover_image_generation() is False


class TestStoryCards:
    @respx.mock
    @pytest.mark.asyncio
    async def test_revise_briefing_embeds_structured_feedback_context(self, llm):
        route = respx.post("http://fake-llm:8000/v1/chat/completions")
        route.side_effect = [
            _chat_response(
                json.dumps(
                    {
                        "cards": [
                            {
                                "main_url": "https://a.com",
                                "what_happened": "A issue",
                                "reference_links": [],
                            }
                        ]
                    }
                )
            ),
            _chat_response("Rewritten draft"),
        ]
        rewritten = await WriterLLM(llm).revise_briefing(
            draft_markdown="Old draft",
            items=[
                {
                    "title": "A issue",
                    "url": "https://a.com",
                    "summary": "A summary",
                    "source_type": "rss",
                }
            ],
            feedback=["Fix link flow"],
            feedback_context={"priority_order": ["Fix hard blockers first"]},
        )
        assert rewritten == "Rewritten draft"
        assert route.call_count == 2

        rewrite_req = json.loads(route.calls[1].request.content)
        rewrite_msg = rewrite_req["messages"][1]["content"]
        assert "Structured feedback context" in rewrite_msg
        assert '"priority_order"' in rewrite_msg


def test_from_settings_role_overrides():
    settings = Settings(
        llm_base_url="http://default-llm:8000/v1",
        llm_model="default-model",
        llm_provider="openai_compat",
        llm_model_writer="writer-model",
        llm_provider_writer="vertexai",
        llm_base_url_writer="https://aiplatform.googleapis.com/v1",
    )
    llm = LLMClient.from_settings(settings)
    assert llm.model_for_role("analyst") == "default-model"
    assert llm.model_for_role("writer") == "writer-model"
    assert llm.endpoint_map()["writer"]["provider"] == "vertexai"


class TestProviderRouting:
    @respx.mock
    @pytest.mark.asyncio
    async def test_analyze_item_uses_role_endpoint(self):
        llm = LLMClient(
            base_url="http://default-llm:8000/v1",
            model="default-model",
            timeout=5,
            role_overrides={
                "analyst": {
                    "base_url": "http://analyst-llm:8000/v1",
                    "model": "analyst-model",
                }
            },
        )
        payload = json.dumps(
            {
                "summary": "ok",
                "relevance_score": 8,
                "tags": ["cloud"],
                "image_prompt": "prompt",
            }
        )
        route = respx.post("http://analyst-llm:8000/v1/chat/completions").mock(
            return_value=_chat_response(payload)
        )
        result = await AnalystLLM(llm).analyze_item("Title", "Body", "url")
        assert result.relevance_score == 8
        assert route.called


def _anthropic_response(text: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "model": "claude-sonnet-4-20250514",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 20},
        },
    )


class TestAnthropicProvider:
    @respx.mock
    @pytest.mark.asyncio
    async def test_basic_anthropic_chat(self):
        llm = LLMClient(
            base_url="https://api.anthropic.com",
            model="claude-sonnet-4-20250514",
            timeout=5,
            provider="anthropic",
            api_key="sk-ant-test",
        )
        route = respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=_anthropic_response("hello from claude")
        )
        result = await llm.chat("system", "user")
        assert result == "hello from claude"
        assert route.called
        req = json.loads(route.calls[0].request.content)
        assert req["model"] == "claude-sonnet-4-20250514"
        assert req["system"] == "system"
        assert req["messages"] == [{"role": "user", "content": "user"}]

    @respx.mock
    @pytest.mark.asyncio
    async def test_anthropic_headers(self):
        llm = LLMClient(
            base_url="https://api.anthropic.com",
            model="claude-sonnet-4-20250514",
            timeout=5,
            provider="anthropic",
            api_key="sk-ant-test-key",
        )
        route = respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=_anthropic_response("ok")
        )
        await llm.chat("sys", "usr")
        headers = route.calls[0].request.headers
        assert headers["x-api-key"] == "sk-ant-test-key"
        assert headers["anthropic-version"] == "2023-06-01"

    @respx.mock
    @pytest.mark.asyncio
    async def test_anthropic_role_override(self):
        llm = LLMClient(
            base_url="http://default:8000/v1",
            model="default-model",
            timeout=5,
            provider="openai_compat",
            role_overrides={
                "writer": {
                    "provider": "anthropic",
                    "base_url": "https://api.anthropic.com",
                    "model": "claude-sonnet-4-20250514",
                    "api_key": "sk-ant-role",
                }
            },
        )
        route = respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=_anthropic_response("from claude writer")
        )
        result = await llm.chat_for_role(
            role="writer", system_prompt="sys", user_content="usr"
        )
        assert result == "from claude writer"
        assert route.called

    @respx.mock
    @pytest.mark.asyncio
    async def test_anthropic_retry_on_429(self):
        llm = LLMClient(
            base_url="https://api.anthropic.com",
            model="claude-sonnet-4-20250514",
            timeout=5,
            provider="anthropic",
            api_key="sk-ant-test",
        )
        route = respx.post("https://api.anthropic.com/v1/messages")
        route.side_effect = [
            httpx.Response(429, json={"error": {"message": "rate limited"}}),
            _anthropic_response("recovered"),
        ]
        result = await llm.chat("sys", "usr", retries=2)
        assert result == "recovered"
        assert route.call_count == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_anthropic_no_text_block_raises(self):
        llm = LLMClient(
            base_url="https://api.anthropic.com",
            model="claude-sonnet-4-20250514",
            timeout=5,
            provider="anthropic",
            api_key="sk-ant-test",
        )
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "msg_test",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": "claude-sonnet-4-20250514",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": 0},
                },
            )
        )
        with pytest.raises(RuntimeError, match="text block"):
            await llm.chat("sys", "usr")

    def test_normalize_provider_anthropic(self):
        assert LLMClient._normalize_provider("anthropic") == "anthropic"
        assert LLMClient._normalize_provider("claude") == "anthropic"
        assert LLMClient._normalize_provider("ANTHROPIC") == "anthropic"
        assert LLMClient._normalize_provider("Claude") == "anthropic"

    def test_from_settings_anthropic(self):
        settings = Settings(
            llm_base_url="https://api.anthropic.com",
            llm_model="claude-sonnet-4-20250514",
            llm_provider="anthropic",
            llm_api_key="sk-ant-test",
        )
        llm = LLMClient.from_settings(settings)
        endpoint = llm.endpoint_map()["default"]
        assert endpoint["provider"] == "anthropic"
        assert endpoint["model"] == "claude-sonnet-4-20250514"

    def test_endpoint_map_masks_anthropic_key(self):
        llm = LLMClient(
            base_url="https://api.anthropic.com",
            model="claude-sonnet-4-20250514",
            timeout=5,
            provider="anthropic",
            api_key="sk-ant-secret",
        )
        emap = llm.endpoint_map()
        assert emap["default"]["api_key"] == "***"
