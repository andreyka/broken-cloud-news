from __future__ import annotations

from collections import Counter
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from unittest.mock import AsyncMock
from unittest.mock import patch
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import pytest
import respx

from bcn.common.config import Settings
from bcn.agents.writer.service import WriterService


def _make_settings(**overrides) -> Settings:
    defaults = dict(
        database_url="postgresql://test:test@localhost:5432/test",
        llm_base_url="http://fake-llm:8000/v1",
        llm_model="test-model",
        comfyui_url="http://fake-comfy:8188",
        github_token="ghp_fake",
        generation_run_stale_pending_minutes=0,
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


class FakeAsyncEventQueue:
    """Async stand-in for EventQueue implementations that require await."""

    def __init__(self):
        self.events: list = []

    async def enqueue_event(self, event):
        self.events.append(event)


def _fake_context(text: str = "collect_all"):
    """Build a minimal RequestContext."""
    from uuid import uuid4

    from a2a.server.agent_execution import RequestContext
    from a2a.types import Message
    from a2a.types import MessageSendParams
    from a2a.types import TextPart

    msg = Message(role="user", parts=[TextPart(text=text)], message_id=uuid4().hex)
    return RequestContext(request=MessageSendParams(message=msg))


def _event_text(event) -> str:
    """Extract human-readable text from one queued agent event."""
    texts: list[str] = []
    for part in getattr(event, "parts", []) or []:
        text = getattr(part, "text", None)
        if isinstance(text, str) and text.strip():
            texts.append(text.strip())
            continue
        root = getattr(part, "root", None)
        root_text = getattr(root, "text", None) if root is not None else None
        if isinstance(root_text, str) and root_text.strip():
            texts.append(root_text.strip())
    return "\n".join(texts)


class TestCliHelpers:
    @pytest.mark.asyncio
    async def test_run_agent_directly_closes_pool_when_close_fails(self):
        from bcn.cli import _run_agent_directly

        class _Executor:
            def __init__(self, settings):
                self.settings = settings

            async def execute(self, context, event_queue):
                return None

            async def close(self):
                raise RuntimeError("close failed")

        settings = _make_settings()
        with (
            patch("bcn.common.db.get_pool", new_callable=AsyncMock),
            patch(
                "bcn.common.db.close_pool", new_callable=AsyncMock
            ) as mock_close_pool,
        ):
            result = await _run_agent_directly(_Executor, settings, "noop")

        assert result == "Done"
        mock_close_pool.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_run_agent_directly_captures_agent_text_message(self):
        from a2a.utils import new_agent_text_message

        from bcn.cli import _run_agent_directly

        class _Executor:
            def __init__(self, settings):
                self.settings = settings

            async def execute(self, context, event_queue):
                event_queue.enqueue_event(
                    new_agent_text_message("Briefing created: id=abc items=1")
                )

            async def close(self):
                return None

        settings = _make_settings()
        with (
            patch("bcn.common.db.get_pool", new_callable=AsyncMock),
            patch("bcn.common.db.close_pool", new_callable=AsyncMock),
        ):
            result = await _run_agent_directly(_Executor, settings, "noop")

        assert result == "Briefing created: id=abc items=1"


# ── Collector tests ──────────────────────────────────────────────────────


class TestCollectorService:
    @respx.mock
    @pytest.mark.asyncio
    async def test_collect_ghsa(self):
        from bcn.agents.collector.service import CollectorService

        settings = _make_settings()
        service = CollectorService(settings)

        respx.post("https://api.github.com/graphql").mock(
            return_value=httpx.Response(
                200,
                json={
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
                                    "identifiers": [
                                        {"type": "CVE", "value": "CVE-2026-0001"}
                                    ],
                                },
                            ]
                        }
                    }
                },
            )
        )

        try:
            items = await service.collect_ghsa_items()
        finally:
            await service.close()

        assert len(items) == 1
        assert items[0].source_type == "ghsa"
        assert items[0].source_id == "GHSA-test-0001"

    @respx.mock
    @pytest.mark.asyncio
    async def test_collect_ghsa_filters_severity(self):
        from bcn.agents.collector.service import CollectorService

        settings = _make_settings()
        service = CollectorService(settings)

        respx.post("https://api.github.com/graphql").mock(
            return_value=httpx.Response(
                200,
                json={
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
                },
            )
        )

        try:
            items = await service.collect_ghsa_items()
        finally:
            await service.close()

        assert items == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_collect_ghsa_drops_items_without_parseable_timestamp(self):
        from bcn.agents.collector.service import CollectorService

        settings = _make_settings()
        service = CollectorService(settings)

        respx.post("https://api.github.com/graphql").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "securityAdvisories": {
                            "nodes": [
                                {
                                    "ghsaId": "GHSA-test-0002",
                                    "summary": "Critical kubernetes vuln",
                                    "description": "A container escape in kubernetes allows...",
                                    "permalink": "https://github.com/advisories/GHSA-test-0002",
                                    "severity": "CRITICAL",
                                    "publishedAt": "",
                                    "references": [],
                                    "identifiers": [],
                                },
                            ]
                        }
                    }
                },
            )
        )

        try:
            items = await service.collect_ghsa_items()
        finally:
            await service.close()

        assert items == []

    @pytest.mark.asyncio
    async def test_collect_rss_uses_feed_publish_timestamp(self):
        from bcn.agents.collector.service import CollectorService

        settings = _make_settings(
            rss_feeds=["https://example.com/security.rss"],
            twitter_required_keywords=["cloud", "kubernetes", "cve"],
        )
        service = CollectorService(settings)

        rss_body = """
        <rss version="2.0"><channel>
          <item>
            <title>Kubernetes CVE write-up</title>
            <link>https://example.com/advisory</link>
            <guid>rss-1</guid>
            <pubDate>Fri, 06 Mar 2026 13:00:01 GMT</pubDate>
            <description>Cloud-native exploit chain details</description>
          </item>
        </channel></rss>
        """

        service.scraper.fetch_text_or_raise = AsyncMock(return_value=rss_body)
        service.scraper.scrape = AsyncMock(return_value="Detailed advisory text")

        try:
            items = await service.collect_rss_items()
        finally:
            await service.close()

        assert len(items) == 1
        assert items[0].published_at == datetime(
            2026, 3, 6, 13, 0, 1, tzinfo=timezone.utc
        )
        assert items[0].raw_data["published"] == "2026-03-06T13:00:01+00:00"
        assert items[0].raw_data["published_field"] == "published_parsed"
        assert items[0].raw_data["published_raw"] is None

    @pytest.mark.asyncio
    async def test_collect_rss_uses_updated_when_published_missing(self):
        from bcn.agents.collector.service import CollectorService

        settings = _make_settings(
            rss_feeds=["https://example.com/security.atom"],
            twitter_required_keywords=["cloud", "kubernetes", "cve"],
        )
        service = CollectorService(settings)

        atom_body = """
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <id>tag:example.com,2026:entry-1</id>
            <title>Cloud container issue</title>
            <updated>2026-03-07T17:30:00Z</updated>
            <summary>Kubernetes container escape write-up</summary>
            <link href="https://example.com/atom-advisory" />
          </entry>
        </feed>
        """

        service.scraper.fetch_text_or_raise = AsyncMock(return_value=atom_body)
        service.scraper.scrape = AsyncMock(return_value="Detailed advisory text")

        try:
            items = await service.collect_rss_items()
        finally:
            await service.close()

        assert len(items) == 1
        assert items[0].published_at == datetime(
            2026, 3, 7, 17, 30, 0, tzinfo=timezone.utc
        )
        assert items[0].raw_data["published"] == "2026-03-07T17:30:00+00:00"
        assert items[0].raw_data["published_field"] == "updated_parsed"
        assert items[0].raw_data["published_raw"] is None

    @pytest.mark.asyncio
    async def test_collect_rss_drops_items_without_parseable_timestamp(self):
        from bcn.agents.collector.service import CollectorService

        settings = _make_settings(
            rss_feeds=["https://example.com/security.rss"],
            twitter_required_keywords=["cloud", "kubernetes", "cve"],
        )
        service = CollectorService(settings)

        rss_body = """
        <rss version="2.0"><channel>
          <item>
            <title>Kubernetes CVE write-up</title>
            <link>https://example.com/advisory</link>
            <guid>rss-1</guid>
            <description>Cloud-native exploit chain details</description>
          </item>
        </channel></rss>
        """

        service.scraper.fetch_text_or_raise = AsyncMock(return_value=rss_body)
        service.scraper.scrape = AsyncMock(return_value="Detailed advisory text")

        try:
            items = await service.collect_rss_items()
        finally:
            await service.close()

        assert items == []

    @pytest.mark.asyncio
    async def test_collect_rss_drops_future_dated_items(self):
        from bcn.agents.collector.service import CollectorService

        settings = _make_settings(
            rss_feeds=["https://example.com/security.rss"],
            twitter_required_keywords=["cloud", "kubernetes", "cve"],
        )
        service = CollectorService(settings)

        rss_body = """
        <rss version="2.0"><channel>
          <item>
            <title>Kubernetes CVE write-up</title>
            <link>https://example.com/advisory</link>
            <guid>rss-1</guid>
            <pubDate>Fri, 06 Mar 2099 13:00:01 GMT</pubDate>
            <description>Cloud-native exploit chain details</description>
          </item>
        </channel></rss>
        """

        service.scraper.fetch_text_or_raise = AsyncMock(return_value=rss_body)
        service.scraper.scrape = AsyncMock(return_value="Detailed advisory text")

        try:
            items = await service.collect_rss_items()
        finally:
            await service.close()

        assert items == []

    @pytest.mark.asyncio
    async def test_collect_reddit(self):
        import json

        from bcn.agents.collector.service import CollectorService

        settings = _make_settings(
            reddit_subreddits=["netsec"],
            twitter_required_keywords=["cloud", "kubernetes", "cve"],
        )
        service = CollectorService(settings)

        rss_body = """
        <rss version="2.0"><channel>
          <item>
            <title>Kubernetes CVE write-up</title>
            <link>https://reddit.com/r/netsec/comments/abc123/test/</link>
            <guid>t3_abc123</guid>
            <pubDate>Mon, 01 Jan 2026 00:00:00 GMT</pubDate>
            <description>Cloud-native exploit chain details</description>
          </item>
        </channel></rss>
        """
        json_body = json.dumps(
            {
                "data": {
                    "children": [
                        {
                            "data": {
                                "id": "abc123",
                                "ups": 120,
                                "num_comments": 42,
                                "upvote_ratio": 0.97,
                                "url": "https://www.stepsecurity.io/blog/hackerbot-claw-github-actions-exploitation",
                                "url_overridden_by_dest": "https://www.stepsecurity.io/blog/hackerbot-claw-github-actions-exploitation",
                                "permalink": "/r/netsec/comments/abc123/test/",
                            }
                        }
                    ]
                }
            }
        )

        async def mock_fetch_text(url, **kwargs):
            if url.endswith(".rss"):
                return 200, rss_body
            if "new.json" in url:
                return 200, json_body
            return 404, ""

        service.scraper.fetch_text = AsyncMock(side_effect=mock_fetch_text)

        try:
            items = await service.collect_reddit_items()
        finally:
            await service.close()

        assert len(items) == 1
        assert items[0].url == "https://reddit.com/r/netsec/comments/abc123/test/"
        assert items[0].published_at == datetime(
            2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc
        )
        raw = items[0].raw_data
        assert raw["published"] == "2026-01-01T00:00:00+00:00"
        assert raw["published_field"] == "published_parsed"
        assert raw["engagement"]["upvotes"] == 120
        assert raw["engagement"]["comments"] == 42
        assert raw["permalink"] == "https://reddit.com/r/netsec/comments/abc123/test/"
        assert raw["references"] == [
            {
                "url": "https://www.stepsecurity.io/blog/hackerbot-claw-github-actions-exploitation"
            }
        ]
        full_content = items[0].full_content or ""
        assert "Reference links:" in full_content
        assert "stepsecurity.io/blog/hackerbot-claw-github-actions-exploitation" in full_content

    @pytest.mark.asyncio
    async def test_collect_reddit_keeps_permalink_for_low_signal_outbound(self):
        import json

        from bcn.agents.collector.service import CollectorService

        settings = _make_settings(
            reddit_subreddits=["netsec"],
            twitter_required_keywords=["cloud", "kubernetes", "cve"],
        )
        service = CollectorService(settings)

        rss_body = """
        <rss version="2.0"><channel>
          <item>
            <title>Cloud community roundup</title>
            <link>https://reddit.com/r/netsec/comments/zzz999/community_thread/</link>
            <guid>t3_zzz999</guid>
            <pubDate>Mon, 01 Jan 2026 00:00:00 GMT</pubDate>
            <description>Cloud chatter and weekly links</description>
          </item>
        </channel></rss>
        """
        json_body = json.dumps(
            {
                "data": {
                    "children": [
                        {
                            "data": {
                                "id": "zzz999",
                                "ups": 51,
                                "num_comments": 7,
                                "upvote_ratio": 0.92,
                                "url": "https://www.youtube.com/watch?v=abc123",
                                "url_overridden_by_dest": "https://www.youtube.com/watch?v=abc123",
                                "permalink": "/r/netsec/comments/zzz999/community_thread/",
                            }
                        }
                    ]
                }
            }
        )

        async def mock_fetch_text(url, **kwargs):
            if url.endswith(".rss"):
                return 200, rss_body
            if "new.json" in url:
                return 200, json_body
            return 404, ""

        service.scraper.fetch_text = AsyncMock(side_effect=mock_fetch_text)

        try:
            items = await service.collect_reddit_items()
        finally:
            await service.close()

        assert len(items) == 1
        assert (
            items[0].url == "https://reddit.com/r/netsec/comments/zzz999/community_thread/"
        )
        raw = items[0].raw_data
        assert raw["references"] == [{"url": "https://www.youtube.com/watch?v=abc123"}]

    def test_extract_tweet_reference_urls_keeps_external_sources(self):
        from bcn.agents.collector.agent import CollectorExecutor

        tweet = {
            "entities": {
                "urls": [
                    {
                        "url": "https://t.co/abc",
                        "expanded_url": "https://x.com/someone/status/123",
                    },
                    {
                        "url": "https://t.co/def",
                        "expanded_url": "https://github.com/org/repo/security/advisories/GHSA-ab12-cd34-ef56",
                    },
                    {
                        "url": "https://t.co/ghi",
                        "unwound_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    },
                ]
            }
        }

        refs = CollectorExecutor._extract_tweet_reference_urls(tweet)
        assert "https://x.com/someone/status/123" not in refs
        assert (
            "https://github.com/org/repo/security/advisories/GHSA-ab12-cd34-ef56"
            in refs
        )
        assert "https://www.youtube.com/watch?v=dQw4w9WgXcQ" in refs

    def test_build_tweet_full_content_appends_reference_links(self):
        from bcn.agents.collector.agent import CollectorExecutor

        content = CollectorExecutor._build_tweet_full_content(
            "Cloud vuln write-up",
            [
                "https://github.com/org/repo",
                "https://www.youtube.com/watch?v=abc123",
            ],
        )
        assert content is not None
        assert "Cloud vuln write-up" in content
        assert "Reference links:" in content
        assert "- https://github.com/org/repo" in content
        assert "- https://www.youtube.com/watch?v=abc123" in content


class TestCollectorExecutor:
    @pytest.mark.asyncio
    async def test_execute_delegates_to_control_plane(self):
        from bcn.agents.collector.agent import CollectorExecutor

        settings = _make_settings()
        executor = CollectorExecutor(settings)

        try:
            with patch(
                "bcn.agents.collector.agent.execute_collection",
                new_callable=AsyncMock,
                return_value="GHSA: collected 1 items",
            ) as mock_execute:
                eq = FakeEventQueue()
                ctx = _fake_context("collect ghsa")
                await executor.execute(ctx, eq)
        finally:
            await executor.close()

        mock_execute.assert_awaited_once_with(
            settings,
            source="ghsa",
            collector_service=executor.service,
            origin="collector_agent",
            manage_pool=False,
        )
        assert any("GHSA: collected 1 items" in str(e) for e in eq.events)

    @pytest.mark.asyncio
    async def test_execute_supports_async_event_queue(self):
        from bcn.agents.collector.agent import CollectorExecutor

        settings = _make_settings()
        executor = CollectorExecutor(settings)

        try:
            with patch(
                "bcn.agents.collector.agent.execute_collection",
                new_callable=AsyncMock,
                return_value="All: GHSA=1, RSS=2, Twitter=3, Reddit=4",
            ):
                eq = FakeAsyncEventQueue()
                ctx = _fake_context("collect")
                await executor.execute(ctx, eq)
        finally:
            await executor.close()

        assert any(
            "All: GHSA=1, RSS=2, Twitter=3, Reddit=4" in str(e) for e in eq.events
        )

    @pytest.mark.asyncio
    async def test_execute_does_not_close_resources_per_request(self):
        from bcn.agents.collector.agent import CollectorExecutor

        settings = _make_settings()
        executor = CollectorExecutor(settings)

        with (
            patch(
                "bcn.agents.collector.agent.execute_collection",
                new_callable=AsyncMock,
                return_value="All: GHSA=0, RSS=0, Twitter=0, Reddit=0",
            ),
            patch.object(
                executor.service, "close", new_callable=AsyncMock
            ) as mock_service_close,
            patch.object(
                executor.scraper, "close", new_callable=AsyncMock
            ) as mock_scraper_close,
            patch.object(
                executor._http, "aclose", new_callable=AsyncMock
            ) as mock_http_close,
        ):
            eq = FakeAsyncEventQueue()
            ctx = _fake_context("collect")
            await executor.execute(ctx, eq)
            mock_service_close.assert_not_awaited()
            mock_scraper_close.assert_not_called()
            mock_http_close.assert_not_called()
            await executor.close()
            mock_service_close.assert_awaited_once()



# ── Analyst tests ────────────────────────────────────────────────────────


class TestAnalystExecutor:
    @pytest.mark.asyncio
    async def test_execute_delegates_to_control_plane(self):
        from bcn.agents.analyst.agent import AnalystExecutor

        settings = _make_settings()
        executor = AnalystExecutor(settings)

        with patch(
            "bcn.agents.analyst.agent.execute_analysis",
            new_callable=AsyncMock,
            return_value="Analyzed 1/1 items",
        ) as mock_execute:
            eq = FakeEventQueue()
            ctx = _fake_context("analyze_new_items")
            await executor.execute(ctx, eq)

        mock_execute.assert_awaited_once_with(
            settings,
            analyst_service=executor.service,
            source="analyst_agent",
            manage_pool=False,
        )
        assert any("1/1" in str(e) for e in eq.events)

    @pytest.mark.asyncio
    async def test_no_items(self):
        from bcn.agents.analyst.agent import AnalystExecutor

        settings = _make_settings()
        executor = AnalystExecutor(settings)

        with patch(
            "bcn.agents.analyst.agent.execute_analysis",
            new_callable=AsyncMock,
            return_value="No new items to analyze",
        ):
            eq = FakeEventQueue()
            ctx = _fake_context("analyze")
            await executor.execute(ctx, eq)

        assert any("No new items" in str(e) for e in eq.events)

    @pytest.mark.asyncio
    async def test_no_items_async_event_queue(self):
        from bcn.agents.analyst.agent import AnalystExecutor

        settings = _make_settings()
        executor = AnalystExecutor(settings)

        with patch(
            "bcn.agents.analyst.agent.execute_analysis",
            new_callable=AsyncMock,
            return_value="No new items to analyze",
        ):
            eq = FakeAsyncEventQueue()
            ctx = _fake_context("analyze")
            await executor.execute(ctx, eq)

        assert any("No new items" in str(e) for e in eq.events)


class TestAnalystService:
    @pytest.mark.asyncio
    async def test_analyze_item_scrapes_reddit_references(self):
        from bcn.agents.analyst.service import AnalystService
        from bcn.common.models import AnalysisResult

        settings = _make_settings()
        service = AnalystService(settings)
        item = {
            "id": "11111111-1111-1111-1111-111111111111",
            "title": "GitHub Actions exploitation rumor",
            "full_content": "submitted by /u/test [link] [comments]",
            "url": "https://www.reddit.com/r/kubernetes/comments/1rhv9pg/hackerbotclaw_ai_bot_exploiting_github_actions/",
            "source_type": "reddit",
            "source_id": "t3_1rhv9pg",
            "raw_data": {
                "references": [
                    {
                        "url": "https://www.stepsecurity.io/blog/hackerbot-claw-github-actions-exploitation"
                    }
                ]
            },
        }

        try:
            with (
                patch.object(
                    service.scraper,
                    "scrape",
                    new_callable=AsyncMock,
                    return_value="Deep technical breakdown from StepSecurity.",
                ) as mock_scrape,
                patch.object(
                    service.analyst_llm,
                    "analyze_item",
                    new_callable=AsyncMock,
                    return_value=AnalysisResult(
                        summary="Pipeline compromise details",
                        relevance_score=8,
                        tags=["github-actions"],
                        image_prompt="cloud security concept art",
                        canonical_url="https://www.stepsecurity.io/blog/hackerbot-claw-github-actions-exploitation",
                    ),
                ) as mock_analyze,
            ):
                result = await service.analyze_item(item)
        finally:
            await service.close()

        mock_scrape.assert_awaited_once_with(
            "https://www.stepsecurity.io/blog/hackerbot-claw-github-actions-exploitation"
        )
        analyze_args = mock_analyze.await_args.args
        assert "Deep technical breakdown from StepSecurity." in analyze_args[1]
        assert result.summary == "Pipeline compromise details"
        assert result.relevance_score == 8
        assert result.ai_tags == ["github-actions"]
        assert (
            result.canonical_url
            == "https://www.stepsecurity.io/blog/hackerbot-claw-github-actions-exploitation"
        )

    @pytest.mark.asyncio
    async def test_analyze_item_raises_on_llm_failure(self):
        from bcn.agents.analyst.service import AnalystService

        settings = _make_settings()
        service = AnalystService(settings)
        item = {
            "id": "11111111-1111-1111-1111-111111111111",
            "title": "K8s escape",
            "full_content": "A container escape vulnerability...",
            "url": "https://example.com",
            "source_type": "rss",
            "source_id": "rss-1",
            "raw_data": {},
        }

        try:
            with patch.object(
                service.analyst_llm,
                "analyze_item",
                new_callable=AsyncMock,
                side_effect=RuntimeError("llm down"),
            ):
                with pytest.raises(RuntimeError):
                    await service.analyze_item(item)
        finally:
            await service.close()


# ── Writer tests ─────────────────────────────────────────────────────────


class TestWriterExecutor:
    def test_resolve_workflow_mode(self):
        from bcn.agents.writer.agent import WriterExecutor

        assert (
            WriterExecutor._resolve_workflow_mode(
                "generate_briefing::regular_monthly_newsletter"
            )
            == "regular_monthly_newsletter"
        )
        assert (
            WriterExecutor._resolve_workflow_mode("generate_briefing")
            == "regular_daily_briefing"
        )

    @pytest.mark.asyncio
    async def test_execute_delegates_to_generation_control_plane(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings()
        executor = WriterExecutor(settings)

        try:
            with patch(
                "bcn.agents.writer.agent.execute_generation",
                new_callable=AsyncMock,
                return_value=(
                    'writer_handoff::{"mode":"regular_monthly_newsletter",'
                    '"decision":"skip","item_count":0}\n'
                    "Monthly newsletter skipped"
                ),
            ) as mock_execute:
                eq = FakeEventQueue()
                ctx = _fake_context("generate_briefing::regular_monthly_newsletter")
                await executor.execute(ctx, eq)
        finally:
            await executor.close()

        mock_execute.assert_awaited_once_with(
            settings,
            mode="regular_monthly_newsletter",
            writer_service=executor.service,
            source="writer_agent",
            manage_pool=False,
        )
        assert any("monthly newsletter skipped" in str(e).lower() for e in eq.events)

    @respx.mock
    @pytest.mark.asyncio
    async def test_no_items(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings()
        executor = WriterExecutor(settings)

        try:
            with patch(
                "bcn.agents.writer.agent.execute_generation",
                new_callable=AsyncMock,
                return_value=(
                    'writer_handoff::{"mode":"regular_daily_briefing",'
                    '"decision":"skip","item_count":0}\n'
                    "Quiet day — no items scored >= 7 in the last 24h. Skipping briefing."
                ),
            ) as mock_execute:
                eq = FakeEventQueue()
                ctx = _fake_context("generate_briefing")
                await executor.execute(ctx, eq)
        finally:
            await executor.close()

        mock_execute.assert_awaited_once()
        assert any("no items" in str(e).lower() for e in eq.events)

    @pytest.mark.asyncio
    async def test_execute_does_not_close_resources_per_request(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings()
        executor = WriterExecutor(settings)

        with (
            patch(
                "bcn.agents.writer.agent.execute_generation",
                new_callable=AsyncMock,
                return_value="writer_handoff::{}",
            ),
            patch.object(
                executor.service, "close", new_callable=AsyncMock
            ) as mock_service_close,
            patch.object(
                executor.llm_client, "close", new_callable=AsyncMock
            ) as mock_llm_close,
            patch.object(
                executor.comfyui, "close", new_callable=AsyncMock
            ) as mock_comfy_close,
        ):
            eq = FakeEventQueue()
            ctx = _fake_context("generate_briefing")
            await executor.execute(ctx, eq)
            mock_service_close.assert_not_awaited()
            await executor.close()
            mock_service_close.assert_awaited_once()

        mock_llm_close.assert_not_called()
        mock_comfy_close.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_supports_async_event_queue(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings()
        executor = WriterExecutor(settings)

        try:
            with patch(
                "bcn.agents.writer.agent.execute_generation",
                new_callable=AsyncMock,
                return_value="writer_handoff::{}\nBriefing created",
            ):
                eq = FakeAsyncEventQueue()
                ctx = _fake_context("generate_briefing")
                await executor.execute(ctx, eq)
        finally:
            await executor.close()

        assert any("Briefing created" in str(e) for e in eq.events)

    def test_selection_limits_single_domain(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings(
            briefing_max_items=5,
            briefing_max_rss_items=5,
            briefing_max_items_per_domain=2,
            briefing_max_ai_items=5,
            briefing_max_twitter_items=5,
        )
        executor = WriterExecutor(settings)

        items = [
            {
                "id": str(uuid4()),
                "title": f"Unit42 item {i}",
                "summary": "Cloud exploit write-up",
                "relevance_score": 10 - i,
                "source_type": "rss",
                "url": f"https://unit42.paloaltonetworks.com/post-{i}/",
                "published_at": datetime.now(timezone.utc).isoformat(),
            }
            for i in range(4)
        ]
        items.extend(
            [
                {
                    "id": str(uuid4()),
                    "title": "Reddit k8s security thread",
                    "summary": "Kubernetes hardening discussion",
                    "relevance_score": 8,
                    "source_type": "reddit",
                    "url": "https://www.reddit.com/r/kubernetes/comments/abc123/thread/",
                    "published_at": datetime.now(timezone.utc).isoformat(),
                },
                {
                    "id": str(uuid4()),
                    "title": "GHSA advisory",
                    "summary": "Patch available for cloud service component",
                    "relevance_score": 8,
                    "source_type": "ghsa",
                    "url": "https://github.com/advisories/GHSA-test-1234",
                    "published_at": datetime.now(timezone.utc).isoformat(),
                },
            ]
        )

        selected = executor._select_items_for_briefing(items)
        domains = Counter(urlparse(str(i["url"])).netloc for i in selected)
        assert domains["unit42.paloaltonetworks.com"] <= 2
        assert len(selected) >= 3

    def test_selection_does_not_force_source_mix(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings(
            briefing_max_items=3,
            briefing_max_items_per_domain=1,
        )
        executor = WriterExecutor(settings)

        items = [
            {
                "id": str(uuid4()),
                "title": "Kubernetes admission bypass in webhook auth flow",
                "summary": "Cluster privilege escalation with concrete patch guidance.",
                "relevance_score": 10,
                "source_type": "rss",
                "url": "https://first.example.com/one",
                "published_at": datetime.now(timezone.utc).isoformat(),
            },
            {
                "id": str(uuid4()),
                "title": "Cloudflare tunnel DNS leak under resolver fallback",
                "summary": "Tenant data exposure through edge resolver caching.",
                "relevance_score": 9,
                "source_type": "rss",
                "url": "https://second.example.com/two",
                "published_at": datetime.now(timezone.utc).isoformat(),
            },
            {
                "id": str(uuid4()),
                "title": "Terraform state secret exposure in remote backend sync",
                "summary": "Credential leak during backend migration.",
                "relevance_score": 8,
                "source_type": "rss",
                "url": "https://third.example.com/three",
                "published_at": datetime.now(timezone.utc).isoformat(),
            },
            {
                "id": str(uuid4()),
                "title": "Lower-ranked reddit thread",
                "summary": "Background discussion only.",
                "relevance_score": 7,
                "source_type": "reddit",
                "url": "https://www.reddit.com/r/netsec/comments/abc123/thread/",
                "published_at": datetime.now(timezone.utc).isoformat(),
            },
        ]

        selected = executor._select_items_for_briefing(items, recent_published=[])

        assert {item["url"] for item in selected} == {
            "https://first.example.com/one",
            "https://second.example.com/two",
            "https://third.example.com/three",
        }

    def test_monthly_selection_dedupes_same_canonical_url(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings(
            monthly_newsletter_min_items=1,
            monthly_newsletter_max_items=3,
            monthly_newsletter_max_items_per_domain=3,
        )
        executor = WriterExecutor(settings)

        primary = {
            "id": str(uuid4()),
            "title": "Cloud metadata bypass in shared build workers",
            "summary": "Primary write-up with patch guidance.",
            "relevance_score": 10,
            "source_type": "rss",
            "url": "https://example.com/path?b=1",
            "published_at": datetime.now(timezone.utc).isoformat(),
        }
        duplicate = {
            "id": str(uuid4()),
            "title": "Same issue from social link",
            "summary": "Tweet linking the same article.",
            "relevance_score": 8,
            "source_type": "twitter",
            "url": "https://www.example.com/path/?utm_source=digest&fbclid=abc&b=1",
            "published_at": datetime.now(timezone.utc).isoformat(),
        }
        distinct = {
            "id": str(uuid4()),
            "title": "Kubernetes secret sync bug leaks bootstrap credentials",
            "summary": "Separate cluster issue.",
            "relevance_score": 9,
            "source_type": "rss",
            "url": "https://other.example.com/secret-sync-leak",
            "published_at": datetime.now(timezone.utc).isoformat(),
        }

        selected = executor._select_items_for_monthly_newsletter(
            [primary, duplicate, distinct]
        )

        assert [item["id"] for item in selected] == [primary["id"], distinct["id"]]

    def test_monthly_selection_dedupes_same_issue_across_urls(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings(
            monthly_newsletter_min_items=1,
            monthly_newsletter_max_items=3,
            monthly_newsletter_max_items_per_domain=3,
            briefing_novelty_title_similarity_threshold=0.99,
        )
        executor = WriterExecutor(settings)

        primary = {
            "id": str(uuid4()),
            "title": "Flowise NVIDIA endpoint auth bypass advisory",
            "summary": "GHSA-5f53-522j-j454 allows unauthenticated access.",
            "relevance_score": 10,
            "source_type": "ghsa",
            "url": "https://github.com/advisories/GHSA-5f53-522j-j454",
            "published_at": datetime.now(timezone.utc).isoformat(),
        }
        duplicate = {
            "id": str(uuid4()),
            "title": "NVIDIA NIM middleware whitelist exposes Flowise endpoints",
            "summary": "Independent post on GHSA-5f53-522j-j454 in Flowise.",
            "relevance_score": 9,
            "source_type": "rss",
            "url": "https://blog.example.com/flowise-nim-auth-bypass",
            "published_at": datetime.now(timezone.utc).isoformat(),
        }
        distinct = {
            "id": str(uuid4()),
            "title": "MLflow auth bypass enables artifact overwrite",
            "summary": "CVE-2025-14297 affects self-hosted deployments.",
            "relevance_score": 8,
            "source_type": "rss",
            "url": "https://tachyon.so/blog/cve-2025-14297-mlflow-authorization-bypass",
            "published_at": datetime.now(timezone.utc).isoformat(),
        }

        selected = executor._select_items_for_monthly_newsletter(
            [primary, duplicate, distinct]
        )

        assert [item["id"] for item in selected] == [primary["id"], distinct["id"]]

    def test_min_selected_fallback_preserves_recent_url_dedup(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings(
            briefing_max_items=3,
            briefing_min_selected_items=1,
            briefing_max_rss_items=3,
            briefing_max_ai_items=3,
            briefing_max_twitter_items=3,
        )
        executor = WriterExecutor(settings)

        items = [
            {
                "id": str(uuid4()),
                "title": "A Race Within A Race: Exploiting CVE-2025-38617 in Linux Packet Sockets https://t.co/abc",
                "summary": "Fresh tweet linking the same Linux packet sockets exploit write-up.",
                "relevance_score": 10,
                "source_type": "twitter",
                "url": "https://blog.calif.io/p/a-race-within-a-race-exploiting-cve",
                "published_at": datetime.now(timezone.utc).isoformat(),
            }
        ]
        recent = [
            {
                "title": "A Race Within A Race: Exploiting CVE-2025-38617 in Linux Packet Sockets",
                "summary": "Previously covered exploit write-up.",
                "url": "https://blog.calif.io/p/a-race-within-a-race-exploiting-cve",
            }
        ]

        selected = executor._select_items_for_briefing(
            items,
            recent_published=recent,
        )

        assert selected == []

    def test_min_selected_fallback_preserves_recent_topic_dedup(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings(
            briefing_max_items=3,
            briefing_min_selected_items=1,
            briefing_max_rss_items=3,
            briefing_max_ai_items=3,
            briefing_max_twitter_items=3,
            briefing_novelty_title_similarity_threshold=0.99,
        )
        executor = WriterExecutor(settings)

        items = [
            {
                "id": str(uuid4()),
                "title": "Kernel exploit chain for CVE-2025-38617 in packet sockets",
                "summary": "New source but same Linux container escape issue.",
                "relevance_score": 10,
                "source_type": "rss",
                "url": "https://different.example.com/linux-packet-sockets-cve-2025-38617",
                "published_at": datetime.now(timezone.utc).isoformat(),
            }
        ]
        recent = [
            {
                "title": "A Race Within A Race: Exploiting CVE-2025-38617 in Linux Packet Sockets",
                "summary": "Previously covered exploit write-up.",
                "url": "https://blog.calif.io/p/a-race-within-a-race-exploiting-cve",
            }
        ]

        selected = executor._select_items_for_briefing(
            items,
            recent_published=recent,
        )

        assert selected == []

    def test_hard_mix_refill_does_not_readd_recent_duplicate(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings(
            briefing_max_items=3,
            briefing_min_selected_items=2,
            briefing_max_rss_items=3,
            briefing_max_ai_items=3,
            briefing_max_twitter_items=3,
            briefing_max_source_share=1.0,
            briefing_selection_require_reddit=False,
            briefing_selection_require_csp=False,
        )
        executor = WriterExecutor(settings)

        items = [
            {
                "id": str(uuid4()),
                "title": "Fresh Kubernetes admission controller bypass",
                "summary": "New cluster auth issue.",
                "relevance_score": 9,
                "source_type": "rss",
                "url": "https://example.com/k8s-admission-bypass",
                "published_at": datetime.now(timezone.utc).isoformat(),
            },
            {
                "id": str(uuid4()),
                "title": "Cloudflare tunnel bug exposes internal DNS responses",
                "summary": "Distinct resolver leakage issue.",
                "relevance_score": 8,
                "source_type": "rss",
                "url": "https://example.com/cloudflare-dns-leak",
                "published_at": datetime.now(timezone.utc).isoformat(),
            },
            {
                "id": str(uuid4()),
                "title": "A Race Within A Race: Exploiting CVE-2025-38617 in Linux Packet Sockets https://t.co/abc",
                "summary": "Fresh tweet linking the same Linux packet sockets exploit write-up.",
                "relevance_score": 10,
                "source_type": "twitter",
                "url": "https://blog.calif.io/p/a-race-within-a-race-exploiting-cve",
                "published_at": datetime.now(timezone.utc).isoformat(),
            },
        ]
        recent = [
            {
                "title": "A Race Within A Race: Exploiting CVE-2025-38617 in Linux Packet Sockets",
                "summary": "Previously covered exploit write-up.",
                "url": "https://blog.calif.io/p/a-race-within-a-race-exploiting-cve",
            }
        ]

        selected = executor._select_items_for_briefing(
            items,
            recent_published=recent,
        )

        assert len(selected) == 2
        assert all(
            item["url"] != "https://blog.calif.io/p/a-race-within-a-race-exploiting-cve"
            for item in selected
        )

    def test_min_selected_fallback_can_still_fill_with_unique_items(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings(
            briefing_max_items=2,
            briefing_min_selected_items=2,
            briefing_max_rss_items=1,
            briefing_max_ai_items=2,
            briefing_max_twitter_items=2,
            briefing_max_source_share=1.0,
        )
        executor = WriterExecutor(settings)

        items = [
            {
                "id": str(uuid4()),
                "title": "Kubernetes bootstrap token leak on worker join path",
                "summary": "Distinct issue one with node enrollment exposure.",
                "relevance_score": 10,
                "source_type": "rss",
                "url": "https://first.example.com/issue-one",
                "published_at": datetime.now(timezone.utc).isoformat(),
            },
            {
                "id": str(uuid4()),
                "title": "Cloudflare tunnel bug exposes internal DNS responses",
                "summary": "Distinct issue two with resolver leakage.",
                "relevance_score": 9,
                "source_type": "rss",
                "url": "https://second.example.com/issue-two",
                "published_at": datetime.now(timezone.utc).isoformat(),
            },
        ]

        selected = executor._select_items_for_briefing(items, recent_published=[])

        assert len(selected) == 2

    def test_detects_missing_urls_in_generated_markdown(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings()
        executor = WriterExecutor(settings)
        items = [
            {"url": "https://example.com/one", "title": "one"},
            {"url": "https://example.com/two", "title": "two"},
        ]
        markdown = "[One](https://example.com/one)\n\nText only."

        missing = executor._missing_items_for_markdown(markdown, items)
        assert len(missing) == 1
        assert missing[0]["url"] == "https://example.com/two"

    def test_missing_urls_uses_canonical_url_key(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings()
        executor = WriterExecutor(settings)
        items = [
            {"url": "https://example.com/path?b=1", "title": "primary"},
            {"url": "https://example.com/other", "title": "other"},
        ]
        markdown = (
            "[Primary]"
            "(https://www.example.com/path/?utm_source=digest&fbclid=abc&b=1)\n\n"
            "Text only."
        )

        missing = executor._missing_items_for_markdown(markdown, items)
        assert len(missing) == 1
        assert missing[0]["url"] == "https://example.com/other"

    def test_novelty_penalty_adds_issue_key_recurrence_penalty(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings(briefing_novelty_title_similarity_threshold=0.99)
        executor = WriterExecutor(settings)
        item = {
            "id": str(uuid4()),
            "title": "Vertex AI notebook chain enables privilege escalation",
            "summary": "GHSA-ab12-cd34-ef56 affects managed pipelines",
            "relevance_score": 8,
            "source_type": "rss",
            "url": "https://example.com/vertex-issue",
        }
        recent_same_issue = [
            {
                "title": "Google advisory for GHSA-ab12-cd34-ef56 in Vertex workflows",
                "summary": "Mitigation guidance for enterprise teams",
                "url": "https://security.example.com/google-vertex-advisory",
            }
        ]
        recent_other_issue = [
            {
                "title": "AWS GuardDuty release improves findings triage",
                "summary": "Platform update with operational notes",
                "url": "https://aws.amazon.com/security/new-feature",
            }
        ]

        overlap_penalty = executor.selector.novelty_penalty(item, recent_same_issue)
        other_penalty = executor.selector.novelty_penalty(item, recent_other_issue)
        assert overlap_penalty > 0.0
        assert overlap_penalty > other_penalty

    def test_duplicate_detection_uses_canonical_url_key(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings()
        executor = WriterExecutor(settings)
        item = {
            "url": "https://example.com/path?b=1",
            "title": "Cloud exploit chain",
        }
        others = [
            {
                "url": "https://www.example.com/path/?utm_source=digest&fbclid=abc&b=1",
                "title": "Different title",
            }
        ]

        assert executor.selector.is_duplicate_of(item, others) is True
        assert executor.selector.novelty_penalty(item, others) >= 3.0

    def test_duplicate_detection_ignores_generic_topic_overlap_without_issue_ids(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings(briefing_novelty_title_similarity_threshold=0.99)
        executor = WriterExecutor(settings)
        item = {
            "url": "https://example.com/flowise-cache-bug",
            "title": "Flowise endpoint middleware cache bug leaks prompts",
            "summary": "Flowise endpoint middleware leak in cache path.",
        }
        others = [
            {
                "url": "https://example.com/flowise-logging-flaw",
                "title": "Flowise endpoint middleware logging flaw exposes tokens",
                "summary": "Flowise endpoint middleware bug in logging path.",
            }
        ]

        assert executor.selector.is_duplicate_of(item, others) is False
        assert executor.selector.novelty_penalty(item, others) == 0.0

    def test_quality_gate_uses_canonical_url_key_for_selected_urls(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings()
        executor = WriterExecutor(settings)
        items = [{"url": "https://example.com/path?b=1", "title": "primary"}]
        markdown = (
            "[Primary]"
            "(https://www.example.com/path/?utm_source=digest&fbclid=abc&b=1)\n\n"
            "Body."
        )

        gate = executor.quality.evaluate(
            markdown,
            items,
            mode="standard",
            min_chars=0,
            hard_max_chars=2000,
        )
        assert not any(
            "Missing selected URL" in issue for issue in gate.get("hard_issues", [])
        )

    def test_quality_gate_blocks_unexpected_urls(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings()
        executor = WriterExecutor(settings)
        items = [{"url": "https://example.com/selected", "title": "selected"}]
        markdown = (
            "[Selected](https://example.com/selected)\n\n"
            "Repeat [Calif](https://blog.calif.io/p/a-race-within-a-race-exploiting-cve)"
        )

        gate = executor._quality_gate(
            markdown,
            items,
            mode="standard",
            min_chars=0,
            hard_max_chars=2000,
        )

        assert any(
            "Unexpected URL not present in selected items" in issue
            for issue in gate.get("hard_issues", [])
        )

    def test_dedupe_markdown_links_uses_canonical_url_key(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings()
        executor = WriterExecutor(settings)
        markdown = (
            "[Primary](https://www.example.com/path/?b=1&utm_source=digest&fbclid=abc)\n"
            "[Duplicate](https://example.com/path?b=1)\n"
            "[Other](https://example.com/path?b=2)"
        )

        deduped = executor._dedupe_markdown_links(markdown)
        assert (
            "[Primary](https://www.example.com/path/?b=1&utm_source=digest&fbclid=abc)"
            in deduped
        )
        assert "[Duplicate](" not in deduped
        assert "Duplicate" in deduped
        assert "[Other](https://example.com/path?b=2)" in deduped

    def test_social_proof_bonus_prioritizes_high_engagement_tweet(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings(
            briefing_social_proof_weight=0.35,
            briefing_social_proof_max_bonus=2.5,
        )
        executor = WriterExecutor(settings)

        low_reddit = {
            "id": str(uuid4()),
            "title": "Low-engagement reddit post",
            "summary": "Cloud note",
            "relevance_score": 8,
            "source_type": "reddit",
            "url": "https://www.reddit.com/r/netsec/comments/aaa111/post/",
            "published_at": datetime.now(timezone.utc).isoformat(),
            "raw_data": {"engagement": {"upvotes": 2, "comments": 0}},
        }
        high_tweet = {
            "id": str(uuid4()),
            "title": "High-engagement tweet",
            "summary": "Cloud exploit chain and fixes",
            "relevance_score": 8,
            "source_type": "twitter",
            "url": "https://x.com/user/status/123",
            "published_at": datetime.now(timezone.utc).isoformat(),
            "raw_data": {
                "public_metrics": {
                    "like_count": 5200,
                    "retweet_count": 1300,
                    "reply_count": 200,
                    "quote_count": 180,
                }
            },
        }

        assert executor._priority_score(high_tweet) > executor._priority_score(
            low_reddit
        )

    def test_source_floor_filters_low_social_noise(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings(
            briefing_min_reddit_engagement_score=40,
            briefing_min_twitter_engagement_score=400,
            briefing_social_floor_exempt_relevance=9,
        )
        executor = WriterExecutor(settings)

        low_reddit = {
            "id": str(uuid4()),
            "title": "Tiny reddit post",
            "summary": "Cloud thought",
            "relevance_score": 7,
            "source_type": "reddit",
            "url": "https://www.reddit.com/r/netsec/comments/aaa111/post/",
            "raw_data": {"engagement": {"upvotes": 2, "comments": 1}},
        }
        high_tweet = {
            "id": str(uuid4()),
            "title": "Popular cloud vuln thread",
            "summary": "Exploit + mitigations",
            "relevance_score": 7,
            "source_type": "twitter",
            "url": "https://x.com/user/status/123",
            "raw_data": {
                "public_metrics": {
                    "like_count": 900,
                    "retweet_count": 120,
                    "reply_count": 30,
                    "quote_count": 10,
                }
            },
        }
        exempt_high_relevance = {
            "id": str(uuid4()),
            "title": "Critical cloud incident report",
            "summary": "High-confidence exploit path and patch",
            "relevance_score": 9,
            "source_type": "reddit",
            "url": "https://www.reddit.com/r/netsec/comments/bbb222/post/",
            "raw_data": {"engagement": {"upvotes": 1, "comments": 0}},
        }

        assert executor._passes_source_floor(low_reddit) is False
        assert executor._passes_source_floor(high_tweet) is True
        assert executor._passes_source_floor(exempt_high_relevance) is True

    def test_quality_gate_flags_missing_urls_and_structure(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings()
        executor = WriterExecutor(settings)
        selected = [
            {"url": "https://example.com/one", "source_type": "ghsa"},
            {"url": "https://example.com/two", "source_type": "rss"},
        ]
        markdown = (
            "**Threat Radar**\n"
            "- [One](https://example.com/one) has exploit details.\n\n"
            "**Operator Moves (next 24h)**\n"
            "- Patch now"
        )

        gate = executor._quality_gate(
            markdown=markdown,
            selected_items=selected,
            mode="standard",
            min_chars=200,
            hard_max_chars=2000,
        )
        issue_text = " ".join(gate["issues"])
        assert gate["passed"] is False
        assert "Missing selected URL" in issue_text

    def test_quality_gate_balanced_mode_keeps_structure_as_soft_feedback(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings(briefing_gate_mode="balanced")
        executor = WriterExecutor(settings)
        selected = [{"url": "https://example.com/one", "source_type": "rss"}]
        markdown = "**Quick Signal**\n[One](https://example.com/one) patch now."

        gate = executor._quality_gate(
            markdown=markdown,
            selected_items=selected,
            mode="standard",
            min_chars=10,
            hard_max_chars=2000,
        )

        assert gate["passed"] is True
        soft_text = " ".join(gate["soft_issues"])
        assert "Too few sections" not in soft_text

    def test_quality_gate_strict_mode_blocks_missing_sections(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings(briefing_gate_mode="strict")
        executor = WriterExecutor(settings)
        selected = [
            {"url": "https://example.com/one", "source_type": "rss"},
            {"url": "https://example.com/two", "source_type": "ghsa"},
        ]
        markdown = "**Quick Signal**\n[One](https://example.com/one) patch now."

        gate = executor._quality_gate(
            markdown=markdown,
            selected_items=selected,
            mode="standard",
            min_chars=10,
            hard_max_chars=2000,
        )

        assert gate["passed"] is False
        hard_text = " ".join(gate["hard_issues"])
        assert "Too few sections" in hard_text

    def test_detemplate_rewrites_detection_and_source_fields(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings()
        executor = WriterExecutor(settings)
        markdown = (
            "**Detection: AI Security at the Edge**\n"
            "Cloudflare blocks unsafe prompts early.\n"
            "*Source: [Cloudflare Blog](https://blog.cloudflare.com/block-unsafe-llm-prompts-with-firewall-for-ai/)*"
        )

        rewritten = executor._de_template_fields(markdown)
        assert "**AI Security at the Edge**" in rewritten
        assert "Detection:" not in rewritten
        assert "Source:" not in rewritten
        assert "reference: [Cloudflare Blog]" in rewritten

    def test_missing_items_fallback_is_readable_without_fixed_heading(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings()
        executor = WriterExecutor(settings)
        markdown = "Core digest body."
        missing = [
            {
                "title": "First extra",
                "summary": "Cloud issue one",
                "url": "https://example.com/one",
            },
            {
                "title": "Second extra",
                "summary": "Cloud issue two",
                "url": "https://example.com/two",
            },
        ]

        out = executor._append_missing_items_section(markdown, missing)
        assert "Additional High-Signal Items" not in out
        assert "[First extra](https://example.com/one)" in out
        assert "[Second extra](https://example.com/two)" in out
        assert "• [Second extra](https://example.com/two)" in out

    def test_strip_unselected_github_advisory_links_keeps_selected_only(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings()
        executor = WriterExecutor(settings)

        selected = [
            {"url": "https://github.com/advisories/GHSA-w6x6-9fp7-fqm4"},
        ]
        markdown = (
            "Bad ref [GHSA-78q6-223p-8x4q](https://github.com/advisories/GHSA-78q6-223p-8x4q)\n\n"
            "Also bad [GHSA-9c6g-9j6q-6w4w](https://github.com/craftcms/cms/security/advisories/GHSA-9c6g-9j6q-6w4w)\n\n"
            "Good ref [GHSA-w6x6-9fp7-fqm4](https://github.com/advisories/GHSA-w6x6-9fp7-fqm4)"
        )

        out = executor._strip_unselected_github_advisory_links(markdown, selected)
        assert "https://github.com/advisories/GHSA-78q6-223p-8x4q" not in out
        assert (
            "https://github.com/craftcms/cms/security/advisories/GHSA-9c6g-9j6q-6w4w"
            not in out
        )
        assert "GHSA-78q6-223p-8x4q" in out
        assert "GHSA-9c6g-9j6q-6w4w" in out
        assert "https://github.com/advisories/GHSA-w6x6-9fp7-fqm4" in out

    def test_strip_unselected_markdown_links_keeps_only_selected_urls(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings()
        executor = WriterExecutor(settings)

        selected = [
            {"url": "https://example.com/selected"},
        ]
        markdown = (
            "Keep [Selected](https://example.com/selected)\n\n"
            "Drop [Repeat](https://blog.calif.io/p/a-race-within-a-race-exploiting-cve)"
        )

        out = executor._strip_unselected_markdown_links(markdown, selected)
        assert "[Selected](https://example.com/selected)" in out
        assert "https://blog.calif.io/p/a-race-within-a-race-exploiting-cve" not in out
        assert "Drop Repeat" in out

    def test_quiet_day_mode_detection(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings(
            briefing_quiet_day_enabled=True,
            briefing_quiet_day_high_signal_threshold=8,
            briefing_quiet_day_min_high_signal_items=3,
        )
        executor = WriterExecutor(settings)

        low_signal_items = [
            {
                "title": "General cloud update",
                "summary": "Not directly actionable",
                "relevance_score": 7,
                "source_type": "rss",
            },
            {
                "title": "Another note",
                "summary": "Context only",
                "relevance_score": 7,
                "source_type": "reddit",
            },
        ]
        high_signal_items = [
            {
                "title": "CVE-2026-1234 auth bypass patch",
                "summary": "Exploit in the wild, mitigation available",
                "relevance_score": 9,
                "source_type": "ghsa",
            },
            {
                "title": "CVE-2026-1111 container escape",
                "summary": "Patch + detections published",
                "relevance_score": 9,
                "source_type": "rss",
            },
            {
                "title": "RCE in cloud control plane",
                "summary": "Fix and IOC guidance",
                "relevance_score": 8,
                "source_type": "twitter",
            },
        ]

        assert executor._is_quiet_day(low_signal_items) is True
        assert executor._is_quiet_day(high_signal_items) is False

    def test_single_item_char_limits_are_relaxed(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings(
            briefing_min_chars=1200,
            briefing_target_chars=1700,
            briefing_hard_max_chars=2300,
            briefing_single_item_min_chars=420,
            briefing_single_item_target_chars=760,
            briefing_single_item_hard_max_chars=1200,
        )
        executor = WriterExecutor(settings)

        min_chars, target_chars, hard_max_chars = executor._char_limits(
            "standard",
            selected_count=1,
        )
        assert min_chars == 420
        assert target_chars == 760
        assert hard_max_chars == 1200

    @pytest.mark.asyncio
    async def test_postprocess_drop_recomputes_missing_urls_before_enrich(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings(
            briefing_missing_coverage_max_drops=1,
            briefing_min_items_after_coverage_drop=1,
        )
        executor = WriterExecutor(settings)
        selected = [
            {
                "id": "one",
                "title": "One",
                "summary": "First item",
                "source_type": "rss",
                "url": "https://example.com/one",
                "relevance_score": 9,
            },
            {
                "id": "two",
                "title": "Two",
                "summary": "Second item",
                "source_type": "rss",
                "url": "https://example.com/two",
                "relevance_score": 7,
            },
        ]
        draft = "**Threat Radar**\n[One](https://example.com/one)\n\nAction now."

        with (
            patch.object(
                executor.writer_llm,
                "enrich_briefing",
                new_callable=AsyncMock,
                return_value=draft,
            ) as mock_enrich,
            patch.object(
                executor,
                "_priority_score",
                side_effect=lambda item: 0 if item["id"] == "two" else 1,
            ),
        ):
            out = await executor._postprocess_briefing(
                briefing_body=draft,
                selected_items=selected,
                mode="standard",
                min_chars=10,
                target_chars=200,
                hard_max_chars=2000,
            )

        # Two enrich attempts from the initial loop only; no extra call with stale dropped URLs.
        assert mock_enrich.await_count == 2
        assert "https://example.com/two" not in out.markdown
        assert [item["id"] for item in out.selected_items] == ["one"]
        assert [item["id"] for item in selected] == ["one", "two"]

    @pytest.mark.asyncio
    async def test_postprocess_appends_missing_items_fallback_when_coverage_stalls(
        self,
    ):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings(
            briefing_missing_coverage_max_drops=0,
        )
        executor = WriterExecutor(settings)
        selected = [
            {
                "id": "one",
                "title": "One",
                "summary": "First item",
                "source_type": "rss",
                "url": "https://example.com/one",
                "relevance_score": 9,
            },
            {
                "id": "two",
                "title": "Two",
                "summary": "Second item",
                "source_type": "rss",
                "url": "https://example.com/two",
                "relevance_score": 8,
            },
        ]
        draft = "**Threat Radar**\n[One](https://example.com/one)\n\nAction now."

        with patch.object(
            executor.writer_llm,
            "enrich_briefing",
            new_callable=AsyncMock,
            return_value=draft,
        ) as mock_enrich:
            out = await executor._postprocess_briefing(
                briefing_body=draft,
                selected_items=selected,
                mode="standard",
                min_chars=10,
                target_chars=200,
                hard_max_chars=2000,
            )

        assert mock_enrich.await_count == 2
        assert "https://example.com/one" in out.markdown
        assert "https://example.com/two" in out.markdown
        assert [item["id"] for item in out.selected_items] == ["one", "two"]

    @pytest.mark.asyncio
    async def test_postprocess_final_hygiene_removes_unselected_ghsa_and_restores_missing_urls(
        self,
    ):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings(
            briefing_missing_coverage_max_drops=0,
        )
        executor = WriterExecutor(settings)
        selected = [
            {
                "id": "one",
                "title": "Craft SSRF",
                "summary": "Official advisory details.",
                "source_type": "ghsa",
                "url": "https://curl.se/libcurl/c/CURLOPT_RESOLVE.html",
                "relevance_score": 9,
            },
            {
                "id": "two",
                "title": "New API DoS",
                "summary": "SQL LIKE wildcard injection DoS advisory.",
                "source_type": "ghsa",
                "url": "https://github.com/advisories/GHSA-w6x6-9fp7-fqm4",
                "relevance_score": 8,
            },
        ]
        draft = (
            "**Threat Radar**\n"
            "Incorrect link [GHSA-78q6-223p-8x4q](https://github.com/advisories/GHSA-78q6-223p-8x4q)\n\n"
            "Action now."
        )

        with patch.object(
            executor.writer_llm,
            "enrich_briefing",
            new_callable=AsyncMock,
            return_value=draft,
        ):
            out = await executor._postprocess_briefing(
                briefing_body=draft,
                selected_items=selected,
                mode="standard",
                min_chars=10,
                target_chars=200,
                hard_max_chars=2000,
            )

        assert "https://github.com/advisories/GHSA-78q6-223p-8x4q" not in out.markdown
        assert "https://curl.se/libcurl/c/CURLOPT_RESOLVE.html" in out.markdown
        assert "https://github.com/advisories/GHSA-w6x6-9fp7-fqm4" in out.markdown

    @pytest.mark.asyncio
    async def test_simulate_briefing_body_returns_string_and_final_selected_items(self):
        settings = _make_settings(
            briefing_missing_coverage_max_drops=1,
            briefing_min_items_after_coverage_drop=1,
            briefing_critique_enabled=False,
        )
        service = WriterService(settings)
        items = [
            {
                "id": "one",
                "title": "One",
                "summary": "First item",
                "source_type": "rss",
                "url": "https://example.com/one",
                "relevance_score": 9,
            },
            {
                "id": "two",
                "title": "Two",
                "summary": "Second item",
                "source_type": "rss",
                "url": "https://example.com/two",
                "relevance_score": 7,
            },
        ]
        draft = "**Threat Radar**\n[One](https://example.com/one)\n\nAction now."

        try:
            with (
                patch.object(
                    service.writer_llm,
                    "generate_briefing",
                    new_callable=AsyncMock,
                    return_value=draft,
                ),
                patch.object(
                    service.writer_llm,
                    "enrich_briefing",
                    new_callable=AsyncMock,
                    return_value=draft,
                ),
                patch.object(
                    service,
                    "priority_score",
                    side_effect=lambda item, recent_published=None: (
                        0 if item["id"] == "two" else 1
                    ),
                ),
            ):
                body, meta = await service.simulate_briefing_body(
                    items,
                    [],
                    apply_critic_rewrites=False,
                )
        finally:
            await service.close()

        assert isinstance(body, str)
        assert "https://example.com/two" not in body
        assert [item["id"] for item in meta["selected_items"]] == ["one"]
        assert [item["id"] for item in items] == ["one", "two"]

    @pytest.mark.asyncio
    async def test_verifier_llm_hard_issues_block_when_configured(self):
        from bcn.briefing.verifier import BriefingFactVerifier

        settings = _make_settings(
            briefing_verifier_block_on_llm_hard=True,
        )
        verifier = BriefingFactVerifier(settings)
        try:
            with (
                patch.object(
                    verifier, "_find_dead_urls", new_callable=AsyncMock, return_value=[]
                ),
                patch.object(
                    verifier, "_top_story_is_ctf_or_event", return_value=False
                ),
                patch.object(
                    verifier.verifier_llm,
                    "verify_briefing_facts",
                    new_callable=AsyncMock,
                    return_value={
                        "passed": False,
                        "score": 42,
                        "hard_issues": ["Subjective top-story preference"],
                        "soft_issues": ["Tone could be tighter"],
                        "recommendations": ["Reorder first section"],
                    },
                ),
            ):
                report = await verifier.evaluate(
                    markdown="**Threat Radar**\n[One](https://example.com/one)",
                    items=[{"url": "https://example.com/one", "title": "One"}],
                )
        finally:
            await verifier.close()

        assert report["passed"] is False
        assert report["blocking_hard_issues"] == []
        assert report["llm_hard_issues"] == ["Subjective top-story preference"]
        assert "Subjective top-story preference" in report["hard_issues"]
        assert report["llm_hard_blocking"] is True

    @pytest.mark.asyncio
    async def test_verifier_blocks_unselected_advisory_mentions(self):
        from bcn.briefing.verifier import BriefingFactVerifier

        settings = _make_settings(
            briefing_verifier_block_on_llm_hard=False,
        )
        verifier = BriefingFactVerifier(settings)
        try:
            with (
                patch.object(
                    verifier, "_find_dead_urls", new_callable=AsyncMock, return_value=[]
                ),
                patch.object(
                    verifier, "_top_story_is_ctf_or_event", return_value=False
                ),
                patch.object(
                    verifier.verifier_llm,
                    "verify_briefing_facts",
                    new_callable=AsyncMock,
                    return_value={
                        "passed": True,
                        "score": 90,
                        "hard_issues": [],
                        "soft_issues": [],
                        "recommendations": [],
                    },
                ),
            ):
                report = await verifier.evaluate(
                    markdown=(
                        "**Threat Radar**\n"
                        "- Wrong reference [GHSA-ab12-cd34-ef56]"
                        "(https://github.com/advisories/GHSA-ab12-cd34-ef56)"
                    ),
                    items=[
                        {
                            "url": "https://github.com/advisories/GHSA-w6x6-9fp7-fqm4",
                            "title": "GHSA-w6x6-9fp7-fqm4",
                            "summary": "Selected advisory",
                        }
                    ],
                )
        finally:
            await verifier.close()

        assert report["passed"] is False
        assert report["llm_hard_blocking"] is False
        assert any(
            "not present in selected items" in issue
            for issue in report["blocking_hard_issues"]
        )
        assert any(
            "Remove references to advisories not present in selected items." in rec
            for rec in report["recommendations"]
        )

    @pytest.mark.asyncio
    async def test_verifier_blocks_unselected_url_mentions(self):
        from bcn.briefing.verifier import BriefingFactVerifier

        settings = _make_settings(
            briefing_verifier_block_on_llm_hard=False,
        )
        verifier = BriefingFactVerifier(settings)
        try:
            with (
                patch.object(
                    verifier, "_find_dead_urls", new_callable=AsyncMock, return_value=[]
                ),
                patch.object(
                    verifier, "_top_story_is_ctf_or_event", return_value=False
                ),
                patch.object(
                    verifier.verifier_llm,
                    "verify_briefing_facts",
                    new_callable=AsyncMock,
                    return_value={
                        "passed": True,
                        "score": 90,
                        "hard_issues": [],
                        "soft_issues": [],
                        "recommendations": [],
                    },
                ),
            ):
                report = await verifier.evaluate(
                    markdown=(
                        "**Threat Radar**\n"
                        "[Selected](https://example.com/selected)\n"
                        "Repeat [Calif](https://blog.calif.io/p/a-race-within-a-race-exploiting-cve)"
                    ),
                    items=[
                        {
                            "url": "https://example.com/selected",
                            "title": "Selected item",
                            "summary": "Selected advisory",
                        }
                    ],
                )
        finally:
            await verifier.close()

        assert report["passed"] is False
        assert any(
            "URLs not present in selected items" in issue
            for issue in report["blocking_hard_issues"]
        )
        assert any(
            "Remove URLs that are not part of the selected items." in rec
            for rec in report["recommendations"]
        )

    def test_passes_critic_thresholds_blocks_critical_issue_terms(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings()
        executor = WriterExecutor(settings)

        critique = {
            "passed": True,
            "score": 96,
            "dimension_scores": {
                "actionability": 95,
                "source_diversity": 94,
                "link_hygiene": 95,
            },
            "issues": ["Factual overreach: claim not in selected items."],
            "recommendations": [],
        }

        assert executor._passes_critic_thresholds(critique) is False

    def test_passes_critic_thresholds_does_not_block_low_source_diversity(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings()
        executor = WriterExecutor(settings)

        critique = {
            "passed": True,
            "score": 96,
            "dimension_scores": {
                "actionability": 95,
                "source_diversity": 10,
                "link_hygiene": 95,
            },
            "issues": [],
            "recommendations": [],
        }

        assert executor._passes_critic_thresholds(critique) is True

    @pytest.mark.asyncio
    async def test_legacy_writer_agent_surfaces_control_plane_failure_message(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings()
        executor = WriterExecutor(settings)

        try:
            with patch(
                "bcn.agents.writer.agent.execute_generation",
                new_callable=AsyncMock,
                return_value=(
                    'writer_handoff::{"mode":"regular_daily_briefing",'
                    '"decision":"blocked","item_count":1}\n'
                    "Blocking publish: internal writer error during generation."
                ),
            ):
                eq = FakeEventQueue()
                ctx = _fake_context("generate_briefing")
                await executor.execute(ctx, eq)
        finally:
            await executor.close()

        assert any("internal writer error" in str(e).lower() for e in eq.events)


# ── Distributor tests ────────────────────────────────────────────────────


class TestDistributorService:
    def test_build_channels_daily_mode_only_telegram_and_discord(self):
        from bcn.agents.distributor.service import DistributorService

        settings = _make_settings(
            telegram_bot_token="123:abc",
            telegram_chat_id="@broken-cloud",
            discord_bot_token="discord-token",
            discord_channel_id="12345",
            smtp_host="smtp.example.com",
            smtp_user="user",
            smtp_password="pass",
            email_from="news@example.com",
            email_recipients=["team@example.com"],
            slack_webhook_url="https://hooks.slack.com/services/T/B/X",
        )
        service = DistributorService(settings)
        channels = service._build_channels(mode="regular_daily_briefing")
        names = [name for name, _channel in channels]
        assert names == ["telegram", "discord"]

    def test_build_channels_monthly_mode_email_only(self):
        from bcn.agents.distributor.service import DistributorService

        settings = _make_settings(
            telegram_bot_token="123:abc",
            telegram_chat_id="@broken-cloud",
            discord_bot_token="discord-token",
            discord_channel_id="12345",
            smtp_host="smtp.example.com",
            smtp_user="user",
            smtp_password="pass",
            email_from="news@example.com",
            email_recipients=["team@example.com"],
        )
        service = DistributorService(settings)
        channels = service._build_channels(
            mode="regular_monthly_newsletter",
            newsletter_recipients=["subscriber@example.com"],
        )
        names = [name for name, _channel in channels]
        assert names == ["email"]

    def test_build_channels_monthly_mode_without_recipients_skips_email(self):
        from bcn.agents.distributor.service import DistributorService

        settings = _make_settings(
            smtp_host="smtp.example.com",
            smtp_user="user",
            smtp_password="pass",
            email_from="news@example.com",
        )
        service = DistributorService(settings)
        channels = service._build_channels(
            mode="regular_monthly_newsletter",
            newsletter_recipients=[],
        )
        assert channels == []

    @pytest.mark.asyncio
    async def test_deliver_returns_no_channels_message(self):
        from bcn.agents.distributor.service import DeliveryRequest
        from bcn.agents.distributor.service import DistributorService

        settings = _make_settings()
        service = DistributorService(settings)

        result = await service.deliver(
            DeliveryRequest(
                briefing={
                    "id": uuid4(),
                    "created_at": datetime.now(timezone.utc),
                    "content_markdown": "**Draft**",
                    "content_html": "<p>Draft</p>",
                    "cover_image_url": "",
                    "item_ids": [],
                },
                mode="regular_daily_briefing",
            )
        )

        assert result.results == {}
        assert result.attempts == ()
        assert result.all_ok is False
        assert result.message == (
            "No distribution channels configured for mode=regular_daily_briefing"
        )

    @pytest.mark.asyncio
    async def test_deliver_partial_channel_failure_returns_incomplete_result(self):
        from bcn.agents.distributor.service import DeliveryRequest
        from bcn.agents.distributor.service import DistributorService

        class _FakeChannel:
            def __init__(self, ok: bool):
                self.ok = ok
                self.sent = 0
                self.closed = 0
                self.last_result = {"primary_message_id": "1"} if ok else {}

            async def send(self, briefing):
                self.sent += 1
                return self.ok

            async def close(self):
                self.closed += 1

        settings = _make_settings()
        service = DistributorService(settings)
        ok_channel = _FakeChannel(True)
        fail_channel = _FakeChannel(False)

        with patch.object(
            service,
            "_build_channels",
            return_value=[("telegram", ok_channel), ("slack", fail_channel)],
        ):
            result = await service.deliver(
                DeliveryRequest(
                    briefing={
                        "id": uuid4(),
                        "created_at": datetime.now(timezone.utc),
                        "content_markdown": "**Draft**",
                        "content_html": "<p>Draft</p>",
                        "cover_image_url": "",
                        "item_ids": [uuid4()],
                    },
                    mode="regular_daily_briefing",
                )
            )

        assert result.results == {"telegram": "ok", "slack": "failed"}
        assert result.all_ok is False
        assert ok_channel.sent == 1
        assert fail_channel.sent == 1
        assert ok_channel.closed == 1
        assert fail_channel.closed == 1
        assert [attempt.channel for attempt in result.attempts] == ["telegram", "slack"]
        assert "incomplete" in result.message.lower()

    @pytest.mark.asyncio
    async def test_deliver_skips_previously_successful_channels(self):
        from bcn.agents.distributor.service import DeliveryRequest
        from bcn.agents.distributor.service import DistributorService

        class _FakeChannel:
            def __init__(self):
                self.sent = 0
                self.closed = 0
                self.last_result = {"primary_message_id": "42"}

            async def send(self, briefing):
                self.sent += 1
                return True

            async def close(self):
                self.closed += 1

        settings = _make_settings()
        service = DistributorService(settings)
        telegram = _FakeChannel()
        slack = _FakeChannel()

        with patch.object(
            service,
            "_build_channels",
            return_value=[("telegram", telegram), ("slack", slack)],
        ):
            result = await service.deliver(
                DeliveryRequest(
                    briefing={
                        "id": uuid4(),
                        "created_at": datetime.now(timezone.utc),
                        "content_markdown": "**Draft**",
                        "content_html": "<p>Draft</p>",
                        "cover_image_url": "",
                        "item_ids": [uuid4()],
                    },
                    mode="regular_daily_briefing",
                    previous_ok_channels=frozenset({"telegram"}),
                )
            )

        assert telegram.sent == 0
        assert slack.sent == 1
        assert telegram.closed == 1
        assert slack.closed == 1
        assert result.results == {"telegram": "ok", "slack": "ok"}
        assert result.all_ok is True
        assert [attempt.channel for attempt in result.attempts] == ["slack"]


class TestDistributorExecutor:
    def test_extract_requested_mode(self):
        from bcn.agents.distributor.agent import DistributorExecutor

        mode = DistributorExecutor._extract_requested_mode(
            "distribute_briefing::123e4567-e89b-12d3-a456-426614174000::regular_monthly_newsletter"
        )
        assert mode == "regular_monthly_newsletter"

    @pytest.mark.asyncio
    async def test_legacy_request_returns_boundary_message(self):
        from bcn.agents.distributor.agent import DistributorExecutor

        settings = _make_settings()
        executor = DistributorExecutor(settings)
        eq = FakeEventQueue()

        await executor.execute(_fake_context("distribute_briefing"), eq)

        assert any(
            "explicit delivery payload" in _event_text(event).lower()
            for event in eq.events
        )

    @pytest.mark.asyncio
    async def test_explicit_delivery_request_emits_structured_result(self):
        from bcn.agents.distributor.agent import DistributorExecutor
        from bcn.agents.distributor.service import ChannelDeliveryResult
        from bcn.agents.distributor.service import DeliveryRequest
        from bcn.agents.distributor.service import DeliveryResult
        from bcn.agents.distributor.service import parse_delivery_result_payload
        from bcn.agents.distributor.service import render_delivery_request_payload

        settings = _make_settings()
        executor = DistributorExecutor(settings)
        briefing_id = uuid4()
        expected = DeliveryResult(
            mode="regular_daily_briefing",
            results={"telegram": "ok"},
            attempts=(
                ChannelDeliveryResult(
                    channel="telegram",
                    status="ok",
                    external_message_id="42",
                    metadata={"primary_message_id": "42"},
                ),
            ),
            all_ok=True,
            message="Distributed to: {'telegram': 'ok'} (mode=regular_daily_briefing)",
        )

        with patch.object(
            executor._service,
            "deliver",
            new_callable=AsyncMock,
            return_value=expected,
        ):
            eq = FakeEventQueue()
            await executor.execute(
                _fake_context(
                    render_delivery_request_payload(
                        DeliveryRequest(
                            briefing={
                                "id": briefing_id,
                                "created_at": datetime.now(timezone.utc),
                                "content_markdown": "**Draft**",
                                "content_html": "<p>Draft</p>",
                                "cover_image_url": "",
                                "item_ids": [],
                            },
                            mode="regular_daily_briefing",
                        )
                    )
                ),
                eq,
            )

        assert len(eq.events) == 1
        text = _event_text(eq.events[0])
        payload = parse_delivery_result_payload(text)
        assert payload == expected
        assert expected.message in text
