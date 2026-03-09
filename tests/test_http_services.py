from __future__ import annotations

import httpx
import pytest
import respx

from bcn.common.config import Settings
from bcn.contracts.review import CritiqueRequest
from bcn.transports.http.server import _headers_authorized
from bcn.transports.http.review import RemoteCriticClient
from bcn.transports.http.server import _build_routes
from bcn.transports.http.writer import RemoteWriterWorkflowClient


def _make_settings(**overrides) -> Settings:
    defaults = {
        "database_url": "postgresql://test:test@localhost:5432/test",
        "llm_base_url": "http://fake-llm:8000/v1",
        "llm_model": "test-model",
        "comfyui_url": "http://fake-comfy:8188",
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.mark.asyncio
@respx.mock
async def test_remote_critic_client_posts_json_request():
    route = respx.post("http://critic.internal/v1/evaluate").mock(
        return_value=httpx.Response(
            200,
            json={
                "source": "cli",
                "critic_passed": True,
                "critic_score": 93,
            },
        )
    )
    client = RemoteCriticClient(
        base_url="http://critic.internal",
        timeout_seconds=30,
        auth_token="shared-token",
    )

    try:
        result = await client.evaluate(
            CritiqueRequest(
                draft_markdown="**Draft**",
                source="cli",
            )
        )
    finally:
        await client.close()

    assert route.called
    assert result["critic_score"] == 93
    assert route.calls.last.request.content
    assert route.calls.last.request.headers["X-BCN-Service-Token"] == "shared-token"


@pytest.mark.asyncio
@respx.mock
async def test_remote_writer_workflow_client_fetches_metadata_and_candidate():
    trace_route = respx.get("http://writer.internal/v1/trace-metadata").mock(
        return_value=httpx.Response(
            200,
            json={
                "llm_model": "writer:model@v1",
                "llm_model_version": "v1",
                "prompts": {"writer": "v1"},
            },
        )
    )
    candidate_route = respx.post(
        "http://writer.internal/v1/generate-release-candidate"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "markdown": "Body",
                "gate": {"passed": True},
                "critique": {"passed": True},
                "critic_threshold_passed": True,
                "verifier": {"passed": True},
                "release_passed": True,
                "rewrites": 0,
                "selected_items": [],
                "rounds": [],
                "preference_pairs": [],
            },
        )
    )
    client = RemoteWriterWorkflowClient(
        base_url="http://writer.internal",
        timeout_seconds=30,
        auth_token="shared-token",
    )

    try:
        trace = await client.get_trace_metadata()
        result = await client.generate_release_candidate(
            selected_items=[],
            history=[],
            mode="standard",
        )
    finally:
        await client.close()

    assert trace_route.called
    assert candidate_route.called
    assert trace.llm_model == "writer:model@v1"
    assert result["release_passed"] is True
    assert candidate_route.calls.last.request.headers["X-BCN-Service-Token"] == "shared-token"


@pytest.mark.asyncio
async def test_component_http_server_serves_critic_requests(monkeypatch):
    class _Critic:
        async def evaluate(self, request):
            return {
                "source": request.source,
                "critic_passed": True,
                "critic_score": 88,
            }

        async def close(self):
            return None

    monkeypatch.setattr(
        "bcn.transports.http.server.build_local_critic_evaluator",
        lambda settings: _Critic(),
    )
    _, post_routes = _build_routes("critic", _make_settings())
    response = await post_routes["/v1/evaluate"](
        {"draft_markdown": "**Draft**", "source": "cli"}
    )

    assert response["critic_score"] == 88
    assert response["source"] == "cli"


def test_component_http_routes_include_versioned_and_legacy_aliases():
    _, post_routes = _build_routes("critic", _make_settings())
    assert "/v1/evaluate" in post_routes
    assert "/evaluate" in post_routes


def test_headers_authorized_accepts_service_token_and_bearer():
    assert _headers_authorized(
        expected_token="shared-token",
        header_token="shared-token",
        authorization_header="",
    )
    assert _headers_authorized(
        expected_token="shared-token",
        header_token="",
        authorization_header="Bearer shared-token",
    )
    assert not _headers_authorized(
        expected_token="shared-token",
        header_token="wrong",
        authorization_header="Bearer wrong",
    )
