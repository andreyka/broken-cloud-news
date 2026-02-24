from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx

from bcn.config import Settings
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


class TestStoryCards:
    @respx.mock
    @pytest.mark.asyncio
    async def test_retry_and_fill_missing_cards(self, llm):
        route = respx.post("http://fake-llm:8000/v1/chat/completions")
        route.side_effect = [
            _chat_response("not-json"),
            _chat_response(
                json.dumps(
                    {
                        "cards": [
                            {
                                "main_url": "https://a.com/path).",
                                "what_happened": "A issue",
                                "reference_links": ["https://b.com/advisory"],
                            }
                        ]
                    }
                )
            ),
        ]
        items = [
            {
                "title": "A issue",
                "url": "https://a.com/path",
                "summary": "A summary",
                "source_type": "rss",
            },
            {
                "title": "B issue",
                "url": "https://b.com/advisory",
                "summary": "B summary",
                "source_type": "ghsa",
            },
        ]
        cards = await llm._build_story_cards(["- item a", "- item b"], items)
        assert route.call_count == 2
        assert [c["main_url"] for c in cards] == [
            "https://a.com/path",
            "https://b.com/advisory",
        ]
        assert cards[1]["what_happened"] == "B summary"

        second_req = json.loads(route.calls[1].request.content)
        assert "Return exactly 2 cards" in second_req["messages"][1]["content"]

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
        rewritten = await llm.revise_briefing(
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
        assert "\"priority_order\"" in rewrite_msg


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
        result = await llm.analyze_item("Title", "Body")
        assert result.relevance_score == 8
        assert route.called

    @respx.mock
    @pytest.mark.asyncio
    async def test_gemini_provider_chat(self):
        llm = LLMClient(
            base_url="https://generativelanguage.googleapis.com/v1beta",
            model="gemini-3.1-pro-preview",
            timeout=5,
            provider="gemini",
            api_key="test-key",
        )
        route = respx.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-pro-preview:generateContent"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "candidates": [
                        {"content": {"parts": [{"text": "hello from gemini"}]}},
                    ]
                },
            )
        )
        result = await llm.chat("system", "user")
        assert result == "hello from gemini"
        assert route.called
        assert route.calls[0].request.url.params.get("key") == "test-key"

    @respx.mock
    @pytest.mark.asyncio
    async def test_vertexai_provider_chat_stream(self):
        llm = LLMClient(
            base_url="https://aiplatform.googleapis.com/v1",
            model="gemini-3.1-pro-preview",
            timeout=5,
            provider="vertexai",
            api_key="test-key",
        )
        route = respx.post(
            "https://aiplatform.googleapis.com/v1/publishers/google/models/gemini-3.1-pro-preview:streamGenerateContent"
        ).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"candidates": [{"content": {"parts": [{"text": "hello"}]}}]},
                    {"candidates": [{"content": {"parts": [{"text": "vertex"}]}}]},
                ],
            )
        )
        result = await llm.chat("system", "user")
        assert result == "hello\nvertex"
        assert route.called
        assert route.calls[0].request.url.params.get("key") == "test-key"

    @respx.mock
    @pytest.mark.asyncio
    async def test_gemini_vertex_provider_chat_stream(self):
        llm = LLMClient(
            base_url="https://aiplatform.googleapis.com/v1",
            model="gemini-3.1-pro-preview",
            timeout=5,
            provider="gemini",
            api_key="test-key",
        )
        route = respx.post(
            "https://aiplatform.googleapis.com/v1/publishers/google/models/gemini-3.1-pro-preview:streamGenerateContent"
        ).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"candidates": [{"content": {"parts": [{"text": "hello"}]}}]},
                    {"candidates": [{"content": {"parts": [{"text": "world"}]}}]},
                ],
            )
        )
        result = await llm.chat("system", "user")
        assert result == "hello\nworld"
        assert route.called
        assert route.calls[0].request.url.params.get("key") == "test-key"

    @respx.mock
    @pytest.mark.asyncio
    async def test_generate_cover_image_data_url(self):
        raw = base64.b64encode(b"\x89PNG\r\n").decode("ascii")
        llm = LLMClient(
            base_url="http://fake-llm:8000/v1",
            model="default-model",
            timeout=5,
            provider="openai_compat",
            role_overrides={
                "cover": {
                    "provider": "gemini",
                    "base_url": "https://aiplatform.googleapis.com/v1",
                    "model": "gemini-3-pro-image-preview",
                    "api_key": "test-key",
                }
            },
        )
        route = respx.post(
            "https://aiplatform.googleapis.com/v1/publishers/google/models/gemini-3-pro-image-preview:streamGenerateContent"
        ).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "candidates": [
                            {
                                "content": {
                                    "parts": [
                                        {"inlineData": {"mimeType": "image/png", "data": raw}},
                                    ]
                                }
                            }
                        ]
                    }
                ],
            )
        )
        assert llm.supports_cover_image_generation() is True
        data_url = await llm.generate_cover_image_data_url("prompt text")
        assert data_url is not None
        assert data_url.startswith("data:image/png;base64,")
        assert route.called

    def test_supports_cover_image_generation_with_vertexai_provider(self):
        llm = LLMClient(
            base_url="http://fake-llm:8000/v1",
            model="default-model",
            timeout=5,
            provider="openai_compat",
            role_overrides={
                "cover": {
                    "provider": "vertexai",
                    "base_url": "https://aiplatform.googleapis.com/v1",
                    "model": "gemini-3-pro-image-preview",
                }
            },
        )
        assert llm.supports_cover_image_generation() is True
