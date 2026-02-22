from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlparse
from unittest.mock import AsyncMock, patch, MagicMock
from uuid import uuid4

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

    @respx.mock
    @pytest.mark.asyncio
    async def test_collect_reddit(self):
        from bcn.agents.collector import CollectorExecutor

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
        respx.get("https://www.reddit.com/r/netsec/.rss").mock(
            return_value=httpx.Response(200, text=rss_body)
        )
        respx.get("https://www.reddit.com/r/netsec/new.json?limit=100").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "children": [
                            {
                                "data": {
                                    "id": "abc123",
                                    "ups": 120,
                                    "num_comments": 42,
                                    "upvote_ratio": 0.97,
                                }
                            }
                        ]
                    }
                },
            )
        )

        with patch("bcn.agents.collector.insert_news_item", new_callable=AsyncMock) as mock_insert:
            from uuid import uuid4
            mock_insert.return_value = uuid4()
            count = await executor._collect_reddit()

        assert count == 1
        mock_insert.assert_called_once()
        raw = mock_insert.call_args.kwargs["raw_data"]
        assert raw["engagement"]["upvotes"] == 120
        assert raw["engagement"]["comments"] == 42


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

        assert any("no items" in str(e).lower() for e in eq.events)

    @pytest.mark.asyncio
    async def test_critique_loop_honors_max_rewrites(self):
        from bcn.agents.writer import WriterExecutor

        settings = _make_settings(
            briefing_critique_enabled=True,
            briefing_critique_max_rounds=5,
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
            patch("bcn.agents.writer.get_analyzed_items", new_callable=AsyncMock, return_value=selected),
            patch("bcn.agents.writer.get_recent_published_items", new_callable=AsyncMock, return_value=[]),
            patch("bcn.agents.writer.get_recent_briefings", new_callable=AsyncMock, return_value=[]),
            patch("bcn.agents.writer.insert_briefing", new_callable=AsyncMock, return_value=uuid4()),
            patch.object(executor, "_select_items_for_briefing", return_value=selected),
            patch.object(executor, "_postprocess_briefing", new_callable=AsyncMock, side_effect=lambda **kw: kw["briefing_body"]),
            patch.object(
                executor,
                "_quality_gate",
                return_value={"passed": True, "hard_issues": [], "soft_issues": [], "issues": []},
            ),
            patch.object(executor.llm, "generate_briefing", new_callable=AsyncMock, return_value="Initial draft"),
            patch.object(
                executor.llm,
                "critique_briefing",
                new_callable=AsyncMock,
                return_value={
                    "passed": False,
                    "score": 40,
                    "issues": ["Needs improvements"],
                    "recommendations": ["Make it stronger"],
                },
            ) as mock_critique,
            patch.object(executor.llm, "revise_briefing", new_callable=AsyncMock, return_value="Rewritten draft") as mock_revise,
            patch.object(executor.llm, "generate_cover_prompt", new_callable=AsyncMock, return_value="cover prompt"),
            patch.object(executor.comfyui, "generate_image", new_callable=AsyncMock, return_value=""),
        ):
            eq = FakeEventQueue()
            ctx = _fake_context("generate_briefing")
            await executor.execute(ctx, eq)

        # initial critique + one critique after each rewrite
        assert mock_critique.await_count == 6
        assert mock_revise.await_count == 5

    def test_selection_limits_single_domain(self):
        from bcn.agents.writer import WriterExecutor

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
        items.extend([
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
        ])

        selected = executor._select_items_for_briefing(items)
        domains = Counter(urlparse(str(i["url"])).netloc for i in selected)
        assert domains["unit42.paloaltonetworks.com"] <= 2
        assert any(i["source_type"] == "reddit" for i in selected)
        assert any(i["source_type"] == "ghsa" for i in selected)

    def test_detects_missing_urls_in_generated_markdown(self):
        from bcn.agents.writer import WriterExecutor

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

    def test_social_proof_bonus_prioritizes_high_engagement_tweet(self):
        from bcn.agents.writer import WriterExecutor

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

        assert executor._priority_score(high_tweet) > executor._priority_score(low_reddit)

    def test_source_floor_filters_low_social_noise(self):
        from bcn.agents.writer import WriterExecutor

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
        from bcn.agents.writer import WriterExecutor

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
        from bcn.agents.writer import WriterExecutor

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
        from bcn.agents.writer import WriterExecutor

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
        from bcn.agents.writer import WriterExecutor

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
        from bcn.agents.writer import WriterExecutor

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
        assert "\n\n[Second extra](https://example.com/two)" in out

    def test_quiet_day_mode_detection(self):
        from bcn.agents.writer import WriterExecutor

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
        from bcn.agents.writer import WriterExecutor

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
