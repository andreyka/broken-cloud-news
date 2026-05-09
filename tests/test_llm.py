from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock
from unittest.mock import patch

import httpx
import pytest
import respx

from bcn.services.analyst.llm import AnalystLLM
from bcn.services.writer.llm import WriterLLM
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


def _tool_call_response(
    *,
    tool_name: str,
    arguments: dict[str, object],
    content: str | None = None,
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "content": content,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ],
                    }
                }
            ]
        },
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

    @respx.mock
    @pytest.mark.asyncio
    async def test_retry_after_header_is_honored(self, llm):
        route = respx.post("http://fake-llm:8000/v1/chat/completions")
        route.side_effect = [
            httpx.Response(
                429,
                json={"error": "quota"},
                headers={"Retry-After": "5"},
            ),
            _chat_response("recovered"),
        ]
        with (
            patch("bcn.common.llm.random.uniform", return_value=0.0),
            patch("bcn.common.llm.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            result = await llm.chat("system", "user", retries=2)

        assert result == "recovered"
        assert route.call_count == 2
        mock_sleep.assert_awaited_once()
        wait_seconds = float(mock_sleep.await_args.args[0])
        assert wait_seconds >= 5.0

    @respx.mock
    @pytest.mark.asyncio
    async def test_openai_compat_tool_loop_executes_tool_and_returns_final_content(self, llm):
        route = respx.post("http://fake-llm:8000/v1/chat/completions")
        route.side_effect = [
            _tool_call_response(
                tool_name="fetch_page_content",
                arguments={"url": "https://example.com"},
            ),
            _chat_response("done"),
        ]

        async def fetch_page_content(url: str) -> str:
            return f"content for {url}"

        result = await llm.chat_for_role(
            role="analyst",
            system_prompt="system",
            user_content="user",
            tools=[fetch_page_content],
        )

        assert result == "done"
        assert route.call_count == 2

        first_request = json.loads(route.calls[0].request.content)
        assert first_request["tools"][0]["function"]["name"] == "fetch_page_content"

        second_request = json.loads(route.calls[1].request.content)
        assert second_request["messages"][2]["role"] == "assistant"
        assert second_request["messages"][3]["role"] == "tool"
        assert second_request["messages"][3]["tool_call_id"] == "call_1"
        assert second_request["messages"][3]["content"] == "content for https://example.com"

    @respx.mock
    @pytest.mark.asyncio
    async def test_qwen_models_disable_thinking_mode(self):
        llm = LLMClient(
            base_url="http://fake-llm:8000/v1",
            model="Qwen/Qwen3.6-35B-A3B-FP8",
            timeout=5,
        )
        route = respx.post("http://fake-llm:8000/v1/chat/completions").mock(
            return_value=_chat_response("ok")
        )

        result = await llm.chat("system", "user")

        assert result == "ok"
        body = json.loads(route.calls[0].request.content)
        assert body["chat_template_kwargs"]["enable_thinking"] is False

    @respx.mock
    @pytest.mark.asyncio
    async def test_non_qwen_models_do_not_set_thinking_override(self, llm):
        route = respx.post("http://fake-llm:8000/v1/chat/completions").mock(
            return_value=_chat_response("ok")
        )

        result = await llm.chat("system", "user")

        assert result == "ok"
        body = json.loads(route.calls[0].request.content)
        assert "chat_template_kwargs" not in body


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


class TestCoverImageGeneration:
    @respx.mock
    @pytest.mark.asyncio
    async def test_openai_image_generation_uses_images_api(self):
        llm = LLMClient(
            base_url="https://api.openai.com/v1",
            model="fallback-model",
            timeout=5,
            role_overrides={
                "cover": {
                    "provider": "openai_compat",
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-image-2",
                    "api_key": "test-key",
                }
            },
        )
        route = respx.post("https://api.openai.com/v1/images/generations").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "b64_json": base64.b64encode(b"png-bytes").decode("ascii")
                        }
                    ]
                },
            )
        )

        writer = WriterLLM(llm)
        assert writer.supports_cover_image_generation() is True
        result = await writer.generate_cover_image_data_url("cover prompt")

        assert result == "data:image/png;base64,cG5nLWJ5dGVz"
        body = json.loads(route.calls[0].request.content)
        assert body["model"] == "gpt-image-2"
        assert body["size"] == "1536x1024"

    def test_openai_image_models_are_detected_for_cover_role(self):
        llm = LLMClient(
            base_url="https://api.openai.com/v1",
            model="fallback-model",
            timeout=5,
            role_overrides={
                "cover": {
                    "provider": "openai_compat",
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-image-2",
                    "api_key": "test-key",
                }
            },
        )

        assert WriterLLM(llm).supports_cover_image_generation() is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_openai_compat_analyze_item_uses_tool_call(self, llm):
        route = respx.post("http://fake-llm:8000/v1/chat/completions")
        route.side_effect = [
            _tool_call_response(
                tool_name="fetch_page_content",
                arguments={"url": "https://example.com"},
            ),
            _chat_response(
                json.dumps(
                    {
                        "summary": "tool-assisted",
                        "relevance_score": 8,
                        "tags": ["cloud"],
                        "image_prompt": "prompt",
                    }
                )
            ),
        ]

        result = await AnalystLLM(llm).analyze_item(
            "Title",
            "Body",
            "https://example.com",
        )

        assert result.summary == "tool-assisted"
        assert result.relevance_score == 8
        assert route.call_count == 2


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
        assert "editorial cover image" in result
        assert "no creepy faces" in result


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


def test_from_settings_retry_tuning():
    settings = Settings(
        llm_base_url="http://default-llm:8000/v1",
        llm_model="default-model",
        llm_chat_retries=20,
        llm_retry_max_wait_seconds=420,
        llm_retry_jitter_min_seconds=0.2,
        llm_retry_jitter_max_seconds=3.0,
    )
    llm = LLMClient.from_settings(settings)
    assert llm.chat_retries == 20
    assert llm.retry_max_wait_seconds == 420
    assert llm.retry_jitter_min_seconds == 0.2
    assert llm.retry_jitter_max_seconds == 3.0


def test_from_settings_analyst_request_policy_overrides():
    settings = Settings(
        llm_base_url="http://default-llm:8000/v1",
        llm_model="default-model",
        llm_timeout=180,
        llm_chat_retries=16,
        llm_retry_max_wait_seconds=600,
        llm_retry_jitter_min_seconds=0.5,
        llm_retry_jitter_max_seconds=5.0,
        llm_timeout_analyst=60,
        llm_chat_retries_analyst=4,
        llm_retry_max_wait_seconds_analyst=45,
        llm_retry_jitter_min_seconds_analyst=0.25,
        llm_retry_jitter_max_seconds_analyst=1.5,
    )
    llm = LLMClient.from_settings(settings)
    assert llm.request_policy_for_role("writer") == {
        "timeout": 180.0,
        "chat_retries": 16,
        "retry_max_wait_seconds": 600.0,
        "retry_jitter_min_seconds": 0.5,
        "retry_jitter_max_seconds": 5.0,
    }
    assert llm.request_policy_for_role("analyst") == {
        "timeout": 60.0,
        "chat_retries": 4,
        "retry_max_wait_seconds": 45.0,
        "retry_jitter_min_seconds": 0.25,
        "retry_jitter_max_seconds": 1.5,
    }


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


@pytest.mark.asyncio
async def test_analyst_request_timeout_is_passed_to_openai_calls():
    llm = LLMClient(
        base_url="http://default-llm:8000/v1",
        model="default-model",
        timeout=30,
        request_policy_overrides={
            "analyst": {
                "timeout": 7,
            }
        },
    )
    llm._client.post = AsyncMock(
        return_value=httpx.Response(
            200,
            request=httpx.Request(
                "POST", "http://default-llm:8000/v1/chat/completions"
            ),
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "summary": "ok",
                                    "relevance_score": 8,
                                    "tags": ["cloud"],
                                    "image_prompt": "prompt",
                                }
                            )
                        }
                    }
                ]
            },
        )
    )

    result = await AnalystLLM(llm).analyze_item("Title", "Body", "https://example.com")

    assert result.relevance_score == 8
    assert llm._client.post.await_args.kwargs["timeout"] == 7


@respx.mock
@pytest.mark.asyncio
async def test_analyst_retries_use_role_specific_budget():
    llm = LLMClient(
        base_url="http://default-llm:8000/v1",
        model="default-model",
        timeout=5,
        chat_retries=6,
        request_policy_overrides={
            "analyst": {
                "chat_retries": 2,
                "retry_jitter_min_seconds": 0.0,
                "retry_jitter_max_seconds": 0.0,
            }
        },
    )
    route = respx.post("http://default-llm:8000/v1/chat/completions").mock(
        side_effect=httpx.ConnectError("fail")
    )
    with patch("bcn.common.llm.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(httpx.ConnectError):
            await AnalystLLM(llm).analyze_item("Title", "Body", "https://example.com")

    assert route.call_count == 2


@pytest.mark.asyncio
async def test_llm_close_closes_cached_genai_clients(monkeypatch):
    import google.genai

    closed: list[str] = []

    class _FakeAio:
        async def aclose(self):
            closed.append("aio")

    class _FakeGenAIClient:
        def __init__(self, **_kwargs):
            self.aio = _FakeAio()

        def close(self):
            closed.append("sync")

    monkeypatch.setattr(google.genai, "Client", _FakeGenAIClient)

    llm = LLMClient(
        base_url="https://aiplatform.googleapis.com/v1",
        model="gemini-3.1-pro-preview",
        timeout=5,
        provider="vertexai",
        api_key="test-key",
    )

    endpoint = llm._endpoint(None)
    first = await llm._get_genai_client(endpoint)
    second = await llm._get_genai_client(endpoint)

    assert first is second
    assert len(llm._genai_clients) == 1

    await llm.close()

    assert closed == ["aio"]
    assert llm._genai_clients == {}
