from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock

import httpx
import pytest
import respx

from bcn.config import Settings


def _make_settings(**overrides) -> Settings:
    defaults = dict(
        database_url="postgresql://test:test@localhost:5432/test",
        llm_base_url="http://fake-llm:8000/v1",
        llm_model="test-model",
        comfyui_url="http://fake-comfy:8188",
        github_token="ghp_fake",
        apify_token="apify_fake",
        browserless_url="http://fake-browserless:3000",
    )
    defaults.update(overrides)
    return Settings(**defaults)


# ── Helpers ──────────────────────────────────────────────────────────────

class FakeEventQueue:
    """Minimal stand-in for EventQueue that captures messages."""

    def __init__(self):
        self.events: list = []

    def enqueue_event(self, event):
        self.events.append(event)


def _fake_context(text: str = "collect_all"):
    """Build a minimal RequestContext."""
    from a2a.server.agent_execution import RequestContext
    from a2a.types import MessageSendParams, Message, TextPart
    from uuid import uuid4

    msg = Message(role="user", parts=[TextPart(text=text)], message_id=uuid4().hex)
    return RequestContext(request=MessageSendParams(message=msg))


# ── Collector tests ──────────────────────────────────────────────────────

class TestCollectorExecutor:
    @respx.mock
    @pytest.mark.asyncio
    async def test_collect_ghsa(self):
        from bcn.agents.collector import CollectorExecutor

        settings = _make_settings()
        executor = CollectorExecutor(settings)

        # Mock GHSA GraphQL endpoint
        respx.post("https://api.github.com/graphql").mock(
            return_value=httpx.Response(200, json={
                "data": {
                    "securityAdvisories": {
                        "nodes": [
                            {
                                "ghsaId": "GHSA-test-0001",
                                "summary": "Critical kubernetes vuln",
                                "description": "A container escape in kubernetes allows...",
                                "permalink": "https://github.com/advisories/GHSA-test-0001",
                                "severity": "CRITICAL",
                                "publishedAt": "2026-01-01T00:00:00Z",
                                "references": [],
                                "identifiers": [{"type": "CVE", "value": "CVE-2026-0001"}],
                            },
                        ]
                    }
                }
            })
        )

        with patch("bcn.agents.collector.insert_news_item", new_callable=AsyncMock) as mock_insert:
            from uuid import uuid4
            mock_insert.return_value = uuid4()
            count = await executor._collect_ghsa()

        assert count == 1
        mock_insert.assert_called_once()
        call_kwargs = mock_insert.call_args
        assert call_kwargs[1]["source_type"] == "ghsa"
        assert call_kwargs[1]["source_id"] == "GHSA-test-0001"

    @respx.mock
    @pytest.mark.asyncio
    async def test_collect_ghsa_filters_severity(self):
        from bcn.agents.collector import CollectorExecutor

        settings = _make_settings()
        executor = CollectorExecutor(settings)

        respx.post("https://api.github.com/graphql").mock(
            return_value=httpx.Response(200, json={
                "data": {
                    "securityAdvisories": {
                        "nodes": [
                            {
                                "ghsaId": "GHSA-low-0001",
                                "summary": "Low severity kubernetes issue",
                                "description": "Minor kubernetes thing",
                                "permalink": "https://github.com/advisories/GHSA-low-0001",
                                "severity": "LOW",
                                "publishedAt": "2026-01-01T00:00:00Z",
                                "references": [],
                                "identifiers": [],
                            },
                        ]
                    }
                }
            })
        )

        with patch("bcn.agents.collector.insert_news_item", new_callable=AsyncMock) as mock_insert:
            count = await executor._collect_ghsa()

        assert count == 0
        mock_insert.assert_not_called()


# ── Analyst tests ────────────────────────────────────────────────────────

class TestAnalystExecutor:
    @respx.mock
    @pytest.mark.asyncio
    async def test_analyze_items(self):
        from bcn.agents.analyst import AnalystExecutor

        settings = _make_settings()
        executor = AnalystExecutor(settings)

        fake_items = [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "title": "K8s escape",
                "full_content": "A container escape vulnerability...",
                "url": "https://example.com",
                "source_type": "ghsa",
                "source_id": "GHSA-test",
                "raw_data": {},
            }
        ]

        analysis_json = json.dumps({
            "summary": "Container escape in k8s",
            "relevance_score": 9,
            "tags": ["k8s"],
            "image_prompt": "cyberpunk",
        })

        respx.post("http://fake-llm:8000/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={
                "choices": [{"message": {"content": analysis_json}}]
            })
        )

        with (
            patch("bcn.agents.analyst.get_new_items", new_callable=AsyncMock, return_value=fake_items),
            patch("bcn.agents.analyst.update_item_analyzed", new_callable=AsyncMock) as mock_update,
        ):
            eq = FakeEventQueue()
            ctx = _fake_context("analyze_new_items")
            await executor.execute(ctx, eq)

        mock_update.assert_called_once()
        assert any("1/1" in str(e) for e in eq.events)

    @respx.mock
    @pytest.mark.asyncio
    async def test_no_items(self):
        from bcn.agents.analyst import AnalystExecutor

        settings = _make_settings()
        executor = AnalystExecutor(settings)

        with patch("bcn.agents.analyst.get_new_items", new_callable=AsyncMock, return_value=[]):
            eq = FakeEventQueue()
            ctx = _fake_context("analyze")
            await executor.execute(ctx, eq)

        assert any("No new items" in str(e) for e in eq.events)


# ── Writer tests ─────────────────────────────────────────────────────────

class TestWriterExecutor:
    @respx.mock
    @pytest.mark.asyncio
    async def test_no_items(self):
        from bcn.agents.writer import WriterExecutor

        settings = _make_settings()
        executor = WriterExecutor(settings)

        with patch("bcn.agents.writer.get_analyzed_items", new_callable=AsyncMock, return_value=[]):
            eq = FakeEventQueue()
            ctx = _fake_context("generate_briefing")
            await executor.execute(ctx, eq)

        assert any("No items" in str(e) for e in eq.events)


# ── Distributor tests ────────────────────────────────────────────────────

class TestDistributorExecutor:
    @pytest.mark.asyncio
    async def test_no_briefing(self):
        from bcn.agents.distributor import DistributorExecutor

        settings = _make_settings()
        executor = DistributorExecutor(settings)

        with patch("bcn.agents.distributor.get_latest_briefing", new_callable=AsyncMock, return_value=None):
            eq = FakeEventQueue()
            ctx = _fake_context("distribute")
            await executor.execute(ctx, eq)

        assert any("No new briefing" in str(e) for e in eq.events)
