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


# ── Collector tests ──────────────────────────────────────────────────────


class TestCollectorExecutor:
    @respx.mock
    @pytest.mark.asyncio
    async def test_collect_ghsa(self):
        from bcn.agents.collector.agent import CollectorExecutor

        settings = _make_settings()
        executor = CollectorExecutor(settings)

        # Mock GHSA GraphQL endpoint
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

        with patch(
            "bcn.agents.collector.agent.insert_news_item", new_callable=AsyncMock
        ) as mock_insert:
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
        from bcn.agents.collector.agent import CollectorExecutor

        settings = _make_settings()
        executor = CollectorExecutor(settings)

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

        with patch(
            "bcn.agents.collector.agent.insert_news_item", new_callable=AsyncMock
        ) as mock_insert:
            count = await executor._collect_ghsa()

        assert count == 0
        mock_insert.assert_not_called()

    @pytest.mark.asyncio
    async def test_collect_reddit(self):
        import json

        from bcn.agents.collector.agent import CollectorExecutor

        settings = _make_settings(
            reddit_subreddits=["netsec"],
            twitter_required_keywords=["cloud", "kubernetes", "cve"],
        )
        executor = CollectorExecutor(settings)

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

        executor.scraper.fetch_text = AsyncMock(side_effect=mock_fetch_text)

        with patch(
            "bcn.agents.collector.agent.insert_news_item", new_callable=AsyncMock
        ) as mock_insert:
            from uuid import uuid4

            mock_insert.return_value = uuid4()
            count = await executor._collect_reddit()

        assert count == 1
        mock_insert.assert_called_once()
        assert (
            mock_insert.call_args.kwargs["url"]
            == "https://reddit.com/r/netsec/comments/abc123/test/"
        )
        raw = mock_insert.call_args.kwargs["raw_data"]
        assert raw["engagement"]["upvotes"] == 120
        assert raw["engagement"]["comments"] == 42
        assert raw["permalink"] == "https://reddit.com/r/netsec/comments/abc123/test/"
        assert raw["references"] == [
            {
                "url": "https://www.stepsecurity.io/blog/hackerbot-claw-github-actions-exploitation"
            }
        ]
        full_content = mock_insert.call_args.kwargs["full_content"] or ""
        assert "Reference links:" in full_content
        assert "stepsecurity.io/blog/hackerbot-claw-github-actions-exploitation" in full_content

    @pytest.mark.asyncio
    async def test_collect_reddit_keeps_permalink_for_low_signal_outbound(self):
        import json

        from bcn.agents.collector.agent import CollectorExecutor

        settings = _make_settings(
            reddit_subreddits=["netsec"],
            twitter_required_keywords=["cloud", "kubernetes", "cve"],
        )
        executor = CollectorExecutor(settings)

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

        executor.scraper.fetch_text = AsyncMock(side_effect=mock_fetch_text)

        with patch(
            "bcn.agents.collector.agent.insert_news_item", new_callable=AsyncMock
        ) as mock_insert:
            from uuid import uuid4

            mock_insert.return_value = uuid4()
            count = await executor._collect_reddit()

        assert count == 1
        mock_insert.assert_called_once()
        assert (
            mock_insert.call_args.kwargs["url"]
            == "https://reddit.com/r/netsec/comments/zzz999/community_thread/"
        )
        raw = mock_insert.call_args.kwargs["raw_data"]
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

    @pytest.mark.asyncio
    async def test_execute_supports_async_event_queue(self):
        from bcn.agents.collector.agent import CollectorExecutor

        settings = _make_settings()
        executor = CollectorExecutor(settings)

        with patch.object(
            executor, "_collect_all", new_callable=AsyncMock, return_value=(1, 2, 3, 4)
        ):
            eq = FakeAsyncEventQueue()
            ctx = _fake_context("collect")
            await executor.execute(ctx, eq)

        assert any(
            "All: GHSA=1, RSS=2, Twitter=3, Reddit=4" in str(e) for e in eq.events
        )

    @pytest.mark.asyncio
    async def test_execute_does_not_close_resources_per_request(self):
        from bcn.agents.collector.agent import CollectorExecutor

        settings = _make_settings()
        executor = CollectorExecutor(settings)

        with (
            patch.object(
                executor,
                "_collect_all",
                new_callable=AsyncMock,
                return_value=(0, 0, 0, 0),
            ),
            patch.object(
                executor.scraper, "close", new_callable=AsyncMock
            ) as mock_scraper_close,
            patch.object(
                executor._http, "aclose", new_callable=AsyncMock
            ) as mock_http_close,
        ):
            eq = FakeEventQueue()
            ctx = _fake_context("collect")
            await executor.execute(ctx, eq)

        mock_scraper_close.assert_not_called()
        mock_http_close.assert_not_called()


# ── Analyst tests ────────────────────────────────────────────────────────


class TestAnalystExecutor:
    @respx.mock
    @pytest.mark.asyncio
    async def test_analyze_items(self):
        from bcn.agents.analyst.agent import AnalystExecutor
        from bcn.common.models import AnalysisResult

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

        with (
            patch(
                "bcn.agents.analyst.agent.get_new_items",
                new_callable=AsyncMock,
                return_value=fake_items,
            ),
            patch(
                "bcn.agents.analyst.agent.update_item_analyzed", new_callable=AsyncMock
            ) as mock_update,
            patch.object(
                executor.analyst_llm,
                "analyze_item",
                new_callable=AsyncMock,
                return_value=AnalysisResult(
                    summary="Container escape in k8s",
                    relevance_score=9,
                    tags=["k8s"],
                    image_prompt="cyberpunk",
                ),
            ),
        ):
            eq = FakeEventQueue()
            ctx = _fake_context("analyze_new_items")
            await executor.execute(ctx, eq)

        mock_update.assert_called_once()
        assert any("1/1" in str(e) for e in eq.events)

    @respx.mock
    @pytest.mark.asyncio
    async def test_no_items(self):
        from bcn.agents.analyst.agent import AnalystExecutor

        settings = _make_settings()
        executor = AnalystExecutor(settings)

        with patch(
            "bcn.agents.analyst.agent.get_new_items",
            new_callable=AsyncMock,
            return_value=[],
        ):
            eq = FakeEventQueue()
            ctx = _fake_context("analyze")
            await executor.execute(ctx, eq)

        assert any("No new items" in str(e) for e in eq.events)

    @pytest.mark.asyncio
    async def test_analyze_item_and_save_scrapes_reddit_references(self):
        from bcn.agents.analyst.agent import AnalystExecutor
        from bcn.common.models import AnalysisResult

        settings = _make_settings()
        executor = AnalystExecutor(settings)
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

        with (
            patch.object(
                executor.scraper,
                "scrape",
                new_callable=AsyncMock,
                return_value="Deep technical breakdown from StepSecurity.",
            ) as mock_scrape,
            patch.object(
                executor.analyst_llm,
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
            patch(
                "bcn.agents.analyst.agent.update_item_analyzed", new_callable=AsyncMock
            ) as mock_update,
        ):
            await executor._analyze_item_and_save(item)

        mock_scrape.assert_awaited_once_with(
            "https://www.stepsecurity.io/blog/hackerbot-claw-github-actions-exploitation"
        )
        analyze_args = mock_analyze.await_args.args
        assert "Deep technical breakdown from StepSecurity." in analyze_args[1]
        mock_update.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_items_async_event_queue(self):
        from bcn.agents.analyst.agent import AnalystExecutor

        settings = _make_settings()
        executor = AnalystExecutor(settings)

        with patch(
            "bcn.agents.analyst.agent.get_new_items",
            new_callable=AsyncMock,
            return_value=[],
        ):
            eq = FakeAsyncEventQueue()
            ctx = _fake_context("analyze")
            await executor.execute(ctx, eq)

        assert any("No new items" in str(e) for e in eq.events)

    @pytest.mark.asyncio
    async def test_execute_reports_failed_items(self):
        from bcn.agents.analyst.agent import AnalystExecutor

        settings = _make_settings()
        executor = AnalystExecutor(settings)
        fake_items = [
            {
                "id": "a",
                "title": "one",
                "full_content": "x",
                "url": "https://example.com/1",
                "source_type": "rss",
                "source_id": "1",
                "raw_data": {},
            },
            {
                "id": "b",
                "title": "two",
                "full_content": "x",
                "url": "https://example.com/2",
                "source_type": "rss",
                "source_id": "2",
                "raw_data": {},
            },
        ]

        with (
            patch(
                "bcn.agents.analyst.agent.get_new_items",
                new_callable=AsyncMock,
                return_value=fake_items,
            ),
            patch.object(
                executor,
                "_analyze_item_and_save",
                new_callable=AsyncMock,
                side_effect=[None, RuntimeError("boom")],
            ),
        ):
            eq = FakeEventQueue()
            ctx = _fake_context("analyze_new_items")
            await executor.execute(ctx, eq)

        assert any("Analyzed 1/2 items (1 failed)" in str(e) for e in eq.events)

    @pytest.mark.asyncio
    async def test_analyze_item_and_save_raises_on_llm_failure(self):
        from bcn.agents.analyst.agent import AnalystExecutor

        settings = _make_settings()
        executor = AnalystExecutor(settings)
        item = {
            "id": "11111111-1111-1111-1111-111111111111",
            "title": "K8s escape",
            "full_content": "A container escape vulnerability...",
            "url": "https://example.com",
            "source_type": "rss",
            "source_id": "rss-1",
            "raw_data": {},
        }

        with (
            patch.object(
                executor.analyst_llm,
                "analyze_item",
                new_callable=AsyncMock,
                side_effect=RuntimeError("llm down"),
            ),
            patch(
                "bcn.agents.analyst.agent.update_item_analyzed", new_callable=AsyncMock
            ) as mock_update,
        ):
            with pytest.raises(RuntimeError):
                await executor._analyze_item_and_save(item)

        mock_update.assert_not_called()


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
    async def test_monthly_mode_uses_period_query(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings()
        executor = WriterExecutor(settings)

        with (
            patch(
                "bcn.agents.writer.agent.get_top_items_for_period",
                new_callable=AsyncMock,
                return_value=[],
            ) as mock_period,
            patch(
                "bcn.agents.writer.agent.get_analyzed_items",
                new_callable=AsyncMock,
            ) as mock_daily,
        ):
            eq = FakeEventQueue()
            ctx = _fake_context("generate_briefing::regular_monthly_newsletter")
            await executor.execute(ctx, eq)

        mock_period.assert_awaited_once()
        mock_daily.assert_not_called()
        assert any("monthly newsletter skipped" in str(e).lower() for e in eq.events)

    @respx.mock
    @pytest.mark.asyncio
    async def test_no_items(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings()
        executor = WriterExecutor(settings)

        with patch(
            "bcn.agents.writer.agent.get_analyzed_items",
            new_callable=AsyncMock,
            return_value=[],
        ):
            eq = FakeEventQueue()
            ctx = _fake_context("generate_briefing")
            await executor.execute(ctx, eq)

        assert any("no items" in str(e).lower() for e in eq.events)

    @pytest.mark.asyncio
    async def test_execute_does_not_close_resources_per_request(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings()
        executor = WriterExecutor(settings)

        with (
            patch(
                "bcn.agents.writer.agent.get_analyzed_items",
                new_callable=AsyncMock,
                return_value=[{"id": "x"}],
            ),
            patch.object(executor, "_execute_core", new_callable=AsyncMock),
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

        mock_llm_close.assert_not_called()
        mock_comfy_close.assert_not_called()

    @pytest.mark.asyncio
    async def test_critique_loop_honors_max_rewrites(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings(
            briefing_critique_enabled=True,
            briefing_critique_max_rounds=5,
            briefing_verifier_enabled=False,
        )
        executor = WriterExecutor(settings)

        selected = [
            {
                "id": str(uuid4()),
                "title": "Cloud issue",
                "summary": "Patch guidance",
                "relevance_score": 9,
                "source_type": "rss",
                "url": "https://example.com/advisory",
                "published_at": datetime.now(timezone.utc).isoformat(),
            }
        ]

        with (
            patch(
                "bcn.agents.writer.agent.get_analyzed_items",
                new_callable=AsyncMock,
                return_value=selected,
            ),
            patch(
                "bcn.agents.writer.agent.get_recent_published_items",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "bcn.agents.writer.agent.get_recent_briefings",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "bcn.agents.writer.agent.insert_briefing",
                new_callable=AsyncMock,
                return_value=uuid4(),
            ),
            patch(
                "bcn.agents.writer.agent.create_generation_run",
                new_callable=AsyncMock,
                return_value=uuid4(),
            ),
            patch(
                "bcn.agents.writer.agent.append_generation_round",
                new_callable=AsyncMock,
            ),
            patch(
                "bcn.agents.writer.agent.insert_generation_preference_pair",
                new_callable=AsyncMock,
            ),
            patch(
                "bcn.agents.writer.agent.finalize_generation_run",
                new_callable=AsyncMock,
            ),
            patch.object(executor, "_select_items_for_briefing", return_value=selected),
            patch.object(
                executor,
                "_postprocess_briefing",
                new_callable=AsyncMock,
                side_effect=lambda **kw: kw["briefing_body"],
            ),
            patch.object(
                executor,
                "_quality_gate",
                return_value={
                    "passed": True,
                    "hard_issues": [],
                    "soft_issues": [],
                    "issues": [],
                },
            ),
            patch.object(
                executor.writer_llm,
                "generate_briefing",
                new_callable=AsyncMock,
                return_value="Initial draft",
            ),
            patch.object(
                executor.critic_llm,
                "critique_briefing",
                new_callable=AsyncMock,
                return_value={
                    "passed": False,
                    "score": 40,
                    "issues": ["Needs improvements"],
                    "recommendations": ["Make it stronger"],
                },
            ) as mock_critique,
            patch.object(
                executor.writer_llm,
                "revise_briefing",
                new_callable=AsyncMock,
                return_value="Rewritten draft",
            ) as mock_revise,
            patch.object(
                executor.writer_llm,
                "generate_cover_prompt",
                new_callable=AsyncMock,
                return_value="cover prompt",
            ),
            patch.object(
                executor.comfyui,
                "generate_image",
                new_callable=AsyncMock,
                return_value="",
            ),
        ):
            eq = FakeEventQueue()
            ctx = _fake_context("generate_briefing")
            await executor.execute(ctx, eq)

        # initial critique + one critique after each rewrite
        assert mock_critique.await_count == 6
        assert mock_revise.await_count == 5
        first_rewrite_kwargs = mock_revise.await_args_list[0].kwargs
        assert "feedback_context" in first_rewrite_kwargs
        feedback_context = first_rewrite_kwargs["feedback_context"]
        assert isinstance(feedback_context, dict)
        assert "blocking" in feedback_context
        assert "critic" in feedback_context
        assert "coverage" in feedback_context

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
        assert any(i["source_type"] == "reddit" for i in selected)
        assert any(i["source_type"] == "ghsa" for i in selected)

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
        assert "https://example.com/two" not in out
        assert len(selected) == 1
        assert selected[0]["id"] == "one"

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
        assert "https://example.com/one" in out
        assert "https://example.com/two" in out

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

        assert "https://github.com/advisories/GHSA-78q6-223p-8x4q" not in out
        assert "https://curl.se/libcurl/c/CURLOPT_RESOLVE.html" in out
        assert "https://github.com/advisories/GHSA-w6x6-9fp7-fqm4" in out

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

    @pytest.mark.asyncio
    async def test_unhandled_writer_error_finalizes_trace_as_blocked(self):
        from bcn.agents.writer.agent import WriterExecutor

        settings = _make_settings(
            briefing_critique_enabled=False,
            briefing_verifier_enabled=False,
        )
        executor = WriterExecutor(settings)
        selected = [
            {
                "id": str(uuid4()),
                "title": "Cloud issue",
                "summary": "Patch guidance",
                "relevance_score": 9,
                "source_type": "rss",
                "url": "https://example.com/advisory",
                "published_at": datetime.now(timezone.utc).isoformat(),
            }
        ]
        run_id = uuid4()

        with (
            patch(
                "bcn.agents.writer.agent.get_analyzed_items",
                new_callable=AsyncMock,
                return_value=selected,
            ),
            patch(
                "bcn.agents.writer.agent.get_recent_published_items",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "bcn.agents.writer.agent.get_recent_briefings",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "bcn.agents.writer.agent.create_generation_run",
                new_callable=AsyncMock,
                return_value=run_id,
            ),
            patch(
                "bcn.agents.writer.agent.finalize_generation_run",
                new_callable=AsyncMock,
            ) as mock_finalize,
            patch(
                "bcn.agents.writer.agent.append_generation_round",
                new_callable=AsyncMock,
            ),
            patch(
                "bcn.agents.writer.agent.insert_briefing",
                new_callable=AsyncMock,
                side_effect=RuntimeError("db down"),
            ),
            patch.object(executor, "_select_items_for_briefing", return_value=selected),
            patch.object(
                executor,
                "_postprocess_briefing",
                new_callable=AsyncMock,
                side_effect=lambda **kw: kw["briefing_body"],
            ),
            patch.object(
                executor,
                "_quality_gate",
                return_value={
                    "passed": True,
                    "hard_issues": [],
                    "soft_issues": [],
                    "issues": [],
                },
            ),
            patch.object(
                executor.writer_llm,
                "generate_briefing",
                new_callable=AsyncMock,
                return_value="**Cloud issue**\n[Ref](https://example.com/advisory)",
            ),
            patch.object(
                executor.writer_llm,
                "generate_cover_prompt",
                new_callable=AsyncMock,
                return_value="cover prompt",
            ),
            patch.object(
                executor.comfyui,
                "generate_image",
                new_callable=AsyncMock,
                return_value="",
            ),
            patch(
                "bcn.agents.writer.agent.finalize_stale_pending_generation_runs",
                new_callable=AsyncMock,
                return_value=0,
            ),
        ):
            eq = FakeEventQueue()
            ctx = _fake_context("generate_briefing")
            await executor.execute(ctx, eq)

        assert mock_finalize.await_count == 1
        assert mock_finalize.await_args.kwargs["run_id"] == run_id
        assert mock_finalize.await_args.kwargs["decision"] == "BLOCKED"
        assert any("internal writer error" in str(e).lower() for e in eq.events)


# ── Distributor tests ────────────────────────────────────────────────────


class TestDistributorExecutor:
    def test_build_channels_daily_mode_only_telegram_and_discord(self):
        from bcn.agents.distributor.agent import DistributorExecutor

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
        executor = DistributorExecutor(settings)
        channels = executor._build_channels(mode="regular_daily_briefing")
        names = [name for name, _channel in channels]
        assert names == ["telegram", "discord"]

    def test_build_channels_monthly_mode_email_only(self):
        from bcn.agents.distributor.agent import DistributorExecutor

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
        executor = DistributorExecutor(settings)
        channels = executor._build_channels(
            mode="regular_monthly_newsletter",
            newsletter_recipients=["subscriber@example.com"],
        )
        names = [name for name, _channel in channels]
        assert names == ["email"]

    def test_build_channels_monthly_mode_without_recipients_skips_email(self):
        from bcn.agents.distributor.agent import DistributorExecutor

        settings = _make_settings(
            smtp_host="smtp.example.com",
            smtp_user="user",
            smtp_password="pass",
            email_from="news@example.com",
        )
        executor = DistributorExecutor(settings)
        channels = executor._build_channels(
            mode="regular_monthly_newsletter",
            newsletter_recipients=[],
        )
        assert channels == []

    def test_extract_requested_mode(self):
        from bcn.agents.distributor.agent import DistributorExecutor

        mode = DistributorExecutor._extract_requested_mode(
            "distribute_briefing::123e4567-e89b-12d3-a456-426614174000::regular_monthly_newsletter"
        )
        assert mode == "regular_monthly_newsletter"

    @pytest.mark.asyncio
    async def test_no_briefing(self):
        from bcn.agents.distributor.agent import DistributorExecutor

        settings = _make_settings()
        executor = DistributorExecutor(settings)

        with patch(
            "bcn.agents.distributor.agent.claim_latest_draft_briefing",
            new_callable=AsyncMock,
            return_value=None,
        ):
            eq = FakeEventQueue()
            ctx = _fake_context("distribute")
            await executor.execute(ctx, eq)

        assert any("No new briefing" in str(e) for e in eq.events)

    @pytest.mark.asyncio
    async def test_claims_requested_briefing_id_when_provided(self):
        from bcn.agents.distributor.agent import DistributorExecutor

        settings = _make_settings()
        executor = DistributorExecutor(settings)
        briefing_id = uuid4()
        briefing = {
            "id": briefing_id,
            "created_at": datetime.now(timezone.utc),
            "content_markdown": "**Draft**",
            "content_html": "<p>Draft</p>",
            "cover_image_url": "",
            "item_ids": [],
        }

        with (
            patch(
                "bcn.agents.distributor.agent.claim_draft_briefing_by_id",
                new_callable=AsyncMock,
                return_value=briefing,
            ) as mock_claim_by_id,
            patch(
                "bcn.agents.distributor.agent.claim_latest_draft_briefing",
                new_callable=AsyncMock,
            ) as mock_claim_latest,
            patch.object(executor, "_build_channels", return_value=[]),
            patch(
                "bcn.agents.distributor.agent.release_briefing_for_retry",
                new_callable=AsyncMock,
            ) as mock_release,
        ):
            eq = FakeEventQueue()
            ctx = _fake_context(f"distribute_briefing::{briefing_id}")
            await executor.execute(ctx, eq)

        mock_claim_by_id.assert_called_once_with(briefing_id)
        mock_claim_latest.assert_not_called()
        mock_release.assert_called_once_with(briefing_id)
        assert any("No distribution channels configured" in str(e) for e in eq.events)

    @pytest.mark.asyncio
    async def test_skips_stale_latest_draft(self):
        from bcn.agents.distributor.agent import DistributorExecutor

        settings = _make_settings(
            briefing_distribution_max_draft_age_minutes=60,
            telegram_bot_token="123:abc",
            telegram_chat_id="@broken-cloud",
        )
        executor = DistributorExecutor(settings)
        stale_created_at = datetime.now(timezone.utc) - timedelta(hours=3)
        stale_briefing = {
            "id": uuid4(),
            "created_at": stale_created_at,
            "content_markdown": "**Old draft**",
            "content_html": "<p>Old draft</p>",
            "cover_image_url": "",
            "item_ids": [],
        }

        with (
            patch(
                "bcn.agents.distributor.agent.claim_latest_draft_briefing",
                new_callable=AsyncMock,
                return_value=stale_briefing,
            ),
            patch(
                "bcn.agents.distributor.agent.mark_briefing_distributed",
                new_callable=AsyncMock,
            ) as mock_mark,
            patch(
                "bcn.agents.distributor.agent.mark_items_published",
                new_callable=AsyncMock,
            ) as mock_publish,
            patch(
                "bcn.agents.distributor.agent.release_briefing_for_retry",
                new_callable=AsyncMock,
            ) as mock_release,
        ):
            eq = FakeEventQueue()
            ctx = _fake_context("distribute")
            await executor.execute(ctx, eq)

        assert any("Latest draft is stale" in str(e) for e in eq.events)
        mock_mark.assert_not_called()
        mock_publish.assert_not_called()
        mock_release.assert_called_once_with(stale_briefing["id"])

    @pytest.mark.asyncio
    async def test_partial_channel_failure_keeps_briefing_draft(self):
        from bcn.agents.distributor.agent import DistributorExecutor

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
        executor = DistributorExecutor(settings)
        briefing = {
            "id": uuid4(),
            "created_at": datetime.now(timezone.utc),
            "content_markdown": "**Draft**",
            "content_html": "<p>Draft</p>",
            "cover_image_url": "",
            "item_ids": [uuid4()],
        }
        ok_channel = _FakeChannel(True)
        fail_channel = _FakeChannel(False)

        with (
            patch(
                "bcn.agents.distributor.agent.claim_latest_draft_briefing",
                new_callable=AsyncMock,
                return_value=briefing,
            ),
            patch(
                "bcn.agents.distributor.agent.get_distribution_outcomes",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch.object(
                executor,
                "_build_channels",
                return_value=[("telegram", ok_channel), ("slack", fail_channel)],
            ),
            patch(
                "bcn.agents.distributor.agent.upsert_distribution_outcome",
                new_callable=AsyncMock,
            ),
            patch(
                "bcn.agents.distributor.agent.mark_briefing_distributed",
                new_callable=AsyncMock,
            ) as mock_mark,
            patch(
                "bcn.agents.distributor.agent.mark_items_published",
                new_callable=AsyncMock,
            ) as mock_publish,
            patch(
                "bcn.agents.distributor.agent.release_briefing_for_retry",
                new_callable=AsyncMock,
            ) as mock_release,
        ):
            eq = FakeEventQueue()
            ctx = _fake_context("distribute")
            await executor.execute(ctx, eq)

        assert any("incomplete" in str(e).lower() for e in eq.events)
        assert ok_channel.sent == 1
        assert fail_channel.sent == 1
        assert ok_channel.closed == 1
        assert fail_channel.closed == 1
        mock_mark.assert_not_called()
        mock_publish.assert_not_called()
        mock_release.assert_called_once_with(briefing["id"])

    @pytest.mark.asyncio
    async def test_skips_previously_successful_channels_and_finishes_distribution(self):
        from bcn.agents.distributor.agent import DistributorExecutor

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
        executor = DistributorExecutor(settings)
        briefing = {
            "id": uuid4(),
            "created_at": datetime.now(timezone.utc),
            "content_markdown": "**Draft**",
            "content_html": "<p>Draft</p>",
            "cover_image_url": "",
            "item_ids": [uuid4()],
        }
        telegram = _FakeChannel()
        slack = _FakeChannel()

        with (
            patch(
                "bcn.agents.distributor.agent.claim_latest_draft_briefing",
                new_callable=AsyncMock,
                return_value=briefing,
            ),
            patch(
                "bcn.agents.distributor.agent.get_distribution_outcomes",
                new_callable=AsyncMock,
                return_value=[{"channel": "telegram", "status": "ok"}],
            ),
            patch.object(
                executor,
                "_build_channels",
                return_value=[("telegram", telegram), ("slack", slack)],
            ),
            patch(
                "bcn.agents.distributor.agent.upsert_distribution_outcome",
                new_callable=AsyncMock,
            ),
            patch(
                "bcn.agents.distributor.agent.mark_briefing_distributed",
                new_callable=AsyncMock,
            ) as mock_mark,
            patch(
                "bcn.agents.distributor.agent.mark_items_published",
                new_callable=AsyncMock,
            ) as mock_publish,
            patch(
                "bcn.agents.distributor.agent.release_briefing_for_retry",
                new_callable=AsyncMock,
            ) as mock_release,
        ):
            eq = FakeEventQueue()
            ctx = _fake_context("distribute")
            await executor.execute(ctx, eq)

        assert telegram.sent == 0
        assert slack.sent == 1
        assert telegram.closed == 1
        assert slack.closed == 1
        mock_mark.assert_called_once()
        mock_publish.assert_called_once()
        mock_release.assert_not_called()
