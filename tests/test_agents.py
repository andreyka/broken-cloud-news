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
from bcn.services.writer.service import WriterService
from bcn.services.writer.models import PostprocessedBriefing


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


# ── Collector tests ──────────────────────────────────────────────────────


class TestCollectorService:
    @respx.mock
    @pytest.mark.asyncio
    async def test_collect_ghsa(self):
        from bcn.services.collector.service import CollectorService

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
        from bcn.services.collector.service import CollectorService

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
        from bcn.services.collector.service import CollectorService

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
        from bcn.services.collector.service import CollectorService

        settings = _make_settings(
            rss_feeds=["https://example.com/security.rss"],
            twitter_required_keywords=["cloud", "kubernetes", "cve"],
            collector_rss_max_item_age_days=365,
        )
        service = CollectorService(settings)

        rss_body = f"""
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
        from bcn.services.collector.service import CollectorService

        settings = _make_settings(
            rss_feeds=["https://example.com/security.atom"],
            twitter_required_keywords=["cloud", "kubernetes", "cve"],
            collector_rss_max_item_age_days=365,
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
        from bcn.services.collector.service import CollectorService

        settings = _make_settings(
            rss_feeds=["https://example.com/security.rss"],
            twitter_required_keywords=["cloud", "kubernetes", "cve"],
        )
        service = CollectorService(settings)

        rss_body = f"""
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
        from bcn.services.collector.service import CollectorService

        settings = _make_settings(
            rss_feeds=["https://example.com/security.rss"],
            twitter_required_keywords=["cloud", "kubernetes", "cve"],
        )
        service = CollectorService(settings)

        rss_body = f"""
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
    async def test_collect_rss_bounds_entry_age_and_full_scrapes_per_feed(self):
        from bcn.services.collector.service import CollectorService

        now = datetime.now(timezone.utc)
        recent_one = (now - timedelta(days=1)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        recent_two = (now - timedelta(days=2)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        old_entry = (now - timedelta(days=120)).strftime("%a, %d %b %Y %H:%M:%S GMT")

        settings = _make_settings(
            rss_feeds=["https://example.com/security.rss"],
            twitter_required_keywords=["cloud", "kubernetes", "cve"],
            collector_rss_max_entries_per_feed=10,
            collector_rss_max_item_age_days=45,
            collector_rss_full_content_limit_per_feed=1,
            collector_rss_scrape_timeout_ms=5000,
        )
        service = CollectorService(settings)

        rss_body = f"""
        <rss version="2.0"><channel>
          <item>
            <title>Kubernetes CVE write-up</title>
            <link>https://example.com/advisory-1</link>
            <guid>rss-1</guid>
            <pubDate>{recent_one}</pubDate>
            <description>Cloud-native exploit chain details</description>
          </item>
          <item>
            <title>Cloud container CVE follow-up</title>
            <link>https://example.com/advisory-2</link>
            <guid>rss-2</guid>
            <pubDate>{recent_two}</pubDate>
            <description>Kubernetes advisory details</description>
          </item>
          <item>
            <title>Old kubernetes CVE archive</title>
            <link>https://example.com/advisory-old</link>
            <guid>rss-old</guid>
            <pubDate>{old_entry}</pubDate>
            <description>Cloud archive details</description>
          </item>
        </channel></rss>
        """

        service.scraper.fetch_text_or_raise = AsyncMock(return_value=rss_body)
        service.scraper.scrape = AsyncMock(return_value="Detailed advisory text")

        try:
            items = await service.collect_rss_items()
        finally:
            await service.close()

        assert len(items) == 2
        assert items[0].full_content == "Detailed advisory text"
        assert items[1].full_content is None
        service.scraper.scrape.assert_awaited_once_with(
            "https://example.com/advisory-1",
            timeout_ms=5000,
            settle_ms=1000,
        )

    @pytest.mark.asyncio
    async def test_collect_reddit(self):
        import json

        from bcn.services.collector.service import CollectorService

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

        from bcn.services.collector.service import CollectorService

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
        from bcn.services.collector.service import CollectorService

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

        refs = CollectorService._extract_tweet_reference_urls(tweet)
        assert "https://x.com/someone/status/123" not in refs
        assert (
            "https://github.com/org/repo/security/advisories/GHSA-ab12-cd34-ef56"
            in refs
        )
        assert "https://www.youtube.com/watch?v=dQw4w9WgXcQ" in refs

    def test_build_tweet_full_content_appends_reference_links(self):
        from bcn.services.collector.service import CollectorService

        content = CollectorService._build_tweet_full_content(
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


# ── Analyst tests ────────────────────────────────────────────────────────


class TestAnalystService:
    @pytest.mark.asyncio
    async def test_analyze_item_scrapes_reddit_references(self):
        from bcn.services.analyst.service import AnalystService
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
        from bcn.services.analyst.service import AnalystService

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


class TestWriterService:
    def test_selection_limits_single_domain(self):
        from bcn.services.writer.service import WriterService

        settings = _make_settings(
            briefing_max_items=5,
            briefing_max_rss_items=5,
            briefing_max_items_per_domain=2,
            briefing_max_ai_items=5,
            briefing_max_twitter_items=5,
        )
        service = WriterService(settings)

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

        selected = service.select_items_for_briefing(items)
        domains = Counter(urlparse(str(i["url"])).netloc for i in selected)
        assert domains["unit42.paloaltonetworks.com"] <= 2
        assert len(selected) >= 3

    def test_selection_does_not_force_source_mix(self):
        from bcn.services.writer.service import WriterService

        settings = _make_settings(
            briefing_max_items=3,
            briefing_max_items_per_domain=1,
        )
        service = WriterService(settings)

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

        selected = service.select_items_for_briefing(items, recent_published=[])

        assert {item["url"] for item in selected} == {
            "https://first.example.com/one",
            "https://second.example.com/two",
            "https://third.example.com/three",
        }

    def test_monthly_selection_dedupes_same_canonical_url(self):
        from bcn.services.writer.service import WriterService

        settings = _make_settings(
            monthly_newsletter_min_items=1,
            monthly_newsletter_max_items=3,
            monthly_newsletter_max_items_per_domain=3,
        )
        service = WriterService(settings)

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

        selected = service.select_items_for_monthly_newsletter(
            [primary, duplicate, distinct]
        )

        assert [item["id"] for item in selected] == [primary["id"], distinct["id"]]

    def test_monthly_selection_dedupes_same_issue_across_urls(self):
        from bcn.services.writer.service import WriterService

        settings = _make_settings(
            monthly_newsletter_min_items=1,
            monthly_newsletter_max_items=3,
            monthly_newsletter_max_items_per_domain=3,
            briefing_novelty_title_similarity_threshold=0.99,
        )
        service = WriterService(settings)

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

        selected = service.select_items_for_monthly_newsletter(
            [primary, duplicate, distinct]
        )

        assert [item["id"] for item in selected] == [primary["id"], distinct["id"]]

    def test_min_selected_fallback_preserves_recent_url_dedup(self):
        from bcn.services.writer.service import WriterService

        settings = _make_settings(
            briefing_max_items=3,
            briefing_min_selected_items=1,
            briefing_max_rss_items=3,
            briefing_max_ai_items=3,
            briefing_max_twitter_items=3,
        )
        service = WriterService(settings)

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

        selected = service.select_items_for_briefing(
            items,
            recent_published=recent,
        )

        assert selected == []

    def test_min_selected_fallback_preserves_recent_topic_dedup(self):
        from bcn.services.writer.service import WriterService

        settings = _make_settings(
            briefing_max_items=3,
            briefing_min_selected_items=1,
            briefing_max_rss_items=3,
            briefing_max_ai_items=3,
            briefing_max_twitter_items=3,
            briefing_novelty_title_similarity_threshold=0.99,
        )
        service = WriterService(settings)

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

        selected = service.select_items_for_briefing(
            items,
            recent_published=recent,
        )

        assert selected == []

    def test_hard_mix_refill_does_not_readd_recent_duplicate(self):
        from bcn.services.writer.service import WriterService

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
        service = WriterService(settings)

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

        selected = service.select_items_for_briefing(
            items,
            recent_published=recent,
        )

        assert len(selected) == 2
        assert all(
            item["url"] != "https://blog.calif.io/p/a-race-within-a-race-exploiting-cve"
            for item in selected
        )

    def test_min_selected_fallback_can_still_fill_with_unique_items(self):
        from bcn.services.writer.service import WriterService

        settings = _make_settings(
            briefing_max_items=2,
            briefing_min_selected_items=2,
            briefing_max_rss_items=1,
            briefing_max_ai_items=2,
            briefing_max_twitter_items=2,
            briefing_max_source_share=1.0,
        )
        service = WriterService(settings)

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

        selected = service.select_items_for_briefing(items, recent_published=[])

        assert len(selected) == 2

    def test_detects_missing_urls_in_generated_markdown(self):
        from bcn.services.writer.service import WriterService

        settings = _make_settings()
        service = WriterService(settings)
        items = [
            {"url": "https://example.com/one", "title": "one"},
            {"url": "https://example.com/two", "title": "two"},
        ]
        markdown = "[One](https://example.com/one)\n\nText only."

        missing = service.missing_items_for_markdown(markdown, items)
        assert len(missing) == 1
        assert missing[0]["url"] == "https://example.com/two"

    def test_missing_urls_uses_canonical_url_key(self):
        from bcn.services.writer.service import WriterService

        settings = _make_settings()
        service = WriterService(settings)
        items = [
            {"url": "https://example.com/path?b=1", "title": "primary"},
            {"url": "https://example.com/other", "title": "other"},
        ]
        markdown = (
            "[Primary]"
            "(https://www.example.com/path/?utm_source=digest&fbclid=abc&b=1)\n\n"
            "Text only."
        )

        missing = service.missing_items_for_markdown(markdown, items)
        assert len(missing) == 1
        assert missing[0]["url"] == "https://example.com/other"

    def test_missing_urls_accept_equivalent_reference_url_for_social_wrapper(self):
        from bcn.services.writer.service import WriterService

        settings = _make_settings()
        service = WriterService(settings)
        items = [
            {
                "url": "https://t.co/abc123",
                "title": "Next.js SSRF write-up https://t.co/abc123",
                "raw_data": {
                    "references": [{"url": "https://github.com/example/nextjs-ssrf"}],
                    "entities": {
                        "urls": [
                            {
                                "url": "https://t.co/abc123",
                                "expanded_url": "https://github.com/example/nextjs-ssrf",
                                "unwound_url": "https://github.com/example/nextjs-ssrf",
                            }
                        ]
                    },
                },
            }
        ]
        markdown = "[repo](https://github.com/example/nextjs-ssrf)"

        missing = service.missing_items_for_markdown(markdown, items)
        assert missing == []

    def test_normalize_section_headings_coerces_bullet_only_digest_into_sections(self):
        from bcn.services.writer.service import WriterService

        settings = _make_settings()
        service = WriterService(settings)
        markdown = (
            "- **Next.js SSRF** — Upgrade now and block absolute-form websocket upgrades.\n\n"
            "- **Exchange Zero-Day** — Patch OWA immediately and hunt for exploited tenants."
        )

        normalized = service.normalize_section_headings(markdown)

        assert "**Next.js SSRF**\nUpgrade now and block absolute-form websocket upgrades." in normalized
        assert "**Exchange Zero-Day**\nPatch OWA immediately and hunt for exploited tenants." in normalized

    def test_novelty_penalty_adds_issue_key_recurrence_penalty(self):
        from bcn.services.writer.service import WriterService

        settings = _make_settings(briefing_novelty_title_similarity_threshold=0.99)
        service = WriterService(settings)
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

        overlap_penalty = service.selector.novelty_penalty(item, recent_same_issue)
        other_penalty = service.selector.novelty_penalty(item, recent_other_issue)
        assert overlap_penalty > 0.0
        assert overlap_penalty > other_penalty

    def test_duplicate_detection_uses_canonical_url_key(self):
        from bcn.services.writer.service import WriterService

        settings = _make_settings()
        service = WriterService(settings)
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

        assert service.selector.is_duplicate_of(item, others) is True
        assert service.selector.novelty_penalty(item, others) >= 3.0

    def test_duplicate_detection_ignores_generic_topic_overlap_without_issue_ids(self):
        from bcn.services.writer.service import WriterService

        settings = _make_settings(briefing_novelty_title_similarity_threshold=0.99)
        service = WriterService(settings)
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

        assert service.selector.is_duplicate_of(item, others) is False
        assert service.selector.novelty_penalty(item, others) == 0.0

    def test_quality_gate_uses_canonical_url_key_for_selected_urls(self):
        from bcn.services.writer.service import WriterService

        settings = _make_settings()
        service = WriterService(settings)
        items = [{"url": "https://example.com/path?b=1", "title": "primary"}]
        markdown = (
            "[Primary]"
            "(https://www.example.com/path/?utm_source=digest&fbclid=abc&b=1)\n\n"
            "Body."
        )

        gate = service.quality.evaluate(
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
        from bcn.services.writer.service import WriterService

        settings = _make_settings()
        service = WriterService(settings)
        items = [{"url": "https://example.com/selected", "title": "selected"}]
        markdown = (
            "[Selected](https://example.com/selected)\n\n"
            "Repeat [Calif](https://blog.calif.io/p/a-race-within-a-race-exploiting-cve)"
        )

        gate = service.quality_gate(
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

    def test_trim_repeated_selected_items_drops_only_history_backed_repeats(self):
        from bcn.services.writer.service import WriterService

        settings = _make_settings()
        service = WriterService(settings)
        pingora = {
            "id": str(uuid4()),
            "title": "Fixing request smuggling vulnerabilities in Pingora OSS deployments",
            "summary": "Cloudflare patched request smuggling in Pingora OSS.",
            "url": "https://blog.cloudflare.com/pingora-oss-smuggling-vulnerabilities/",
        }
        cisco = {
            "id": str(uuid4()),
            "title": "CVE-2026-20127 (Cisco SD-WAN, CVSS 10) has been actively exploited since 2023",
            "summary": "PatchIntel breakdown of the active exploitation campaign.",
            "url": "https://patchintel.substack.com/p/patchintel-issue-1-week-of-march?r=7v8ayn",
        }
        budibase = {
            "id": str(uuid4()),
            "title": "@budibase/server: Command Injection in PostgreSQL Dump Command",
            "summary": "GHSA-726g-59wr-cj4c impacts backup export handling.",
            "url": "https://github.com/advisories/GHSA-726g-59wr-cj4c",
        }
        history = [
            {
                "content_markdown": (
                    "**Smuggling Mess**\n"
                    "[Pingora vulnerable to HTTP Request Smuggling via Premature Upgrade]"
                    "(https://github.com/cloudflare/pingora/security/advisories/GHSA-xq2h-p299-vjwv)\n\n"
                    "**SD-WAN Fire**\n"
                    "[Cisco flags more SD-WAN flaws as actively exploited in attacks]"
                    "(https://www.bleepingcomputer.com/news/security/cisco-flags-more-sd-wan-flaws-as-actively-exploited-in-attacks/)"
                )
            }
        ]
        critique = {
            "issues": [
                "Repeated topic: Pingora HTTP request smuggling (already covered in briefing 4)",
                "Repeated topic: Cisco SD-WAN active exploitation (already covered in briefing 5)",
            ]
        }

        trimmed = service.trim_repeated_selected_items(
            selected_items=[pingora, cisco, budibase],
            critique=critique,
            history=history,
        )

        assert [item["id"] for item in trimmed["selected_items"]] == [budibase["id"]]
        assert {item["id"] for item in trimmed["dropped_items"]} == {
            pingora["id"],
            cisco["id"],
        }

    def test_trim_repeated_selected_items_can_drop_below_minimum_when_needed(self):
        from bcn.services.writer.service import WriterService

        settings = _make_settings(briefing_min_selected_items=5)
        service = WriterService(settings)
        cisco = {
            "id": str(uuid4()),
            "title": "CVE-2026-20127 (Cisco SD-WAN, CVSS 10) has been actively exploited since 2023",
            "summary": "PatchIntel breakdown of the active exploitation campaign.",
            "url": "https://patchintel.substack.com/p/patchintel-issue-1-week-of-march?r=7v8ayn",
        }
        kept_items = [
            {
                "id": str(uuid4()),
                "title": "Nextcloud's Key Under the Mat Moment",
                "summary": "Path traversal exposes environment secrets.",
                "url": "https://threatroad.substack.com/p/nextclouds-key-under-the-mat-moment",
            },
            {
                "id": str(uuid4()),
                "title": "ShinyHunters claims more high-profile victims in latest Salesforce customers data heist",
                "summary": "Guest IAM profiles expose Salesforce data.",
                "url": "https://www.theregister.com/2026/03/09/shinyhunters_claims_more_highprofile_victims/",
            },
            {
                "id": str(uuid4()),
                "title": "Glances Exposes Unauthenticated Configuration Secrets",
                "summary": "Glances dumps config without auth.",
                "url": "https://github.com/nicolargo/glances/security/advisories/GHSA-gh4x-f7cq-wwx6",
            },
            {
                "id": str(uuid4()),
                "title": "@budibase/server: Command Injection in PostgreSQL Dump Command",
                "summary": "GHSA-726g-59wr-cj4c impacts backup export handling.",
                "url": "https://github.com/advisories/GHSA-726g-59wr-cj4c",
            },
        ]
        history = [
            {
                "content_markdown": (
                    "[Cisco flags more SD-WAN flaws as actively exploited in attacks]"
                    "(https://www.bleepingcomputer.com/news/security/cisco-flags-more-sd-wan-flaws-as-actively-exploited-in-attacks/)"
                )
            }
        ]
        critique = {
            "issues": [
                "Repeated topic: Cisco SD-WAN active exploitation (already covered in briefing 5)",
            ]
        }

        trimmed = service.trim_repeated_selected_items(
            selected_items=[cisco, *kept_items],
            critique=critique,
            history=history,
        )

        assert [item["id"] for item in trimmed["selected_items"]] == [
            item["id"] for item in kept_items
        ]
        assert [item["id"] for item in trimmed["dropped_items"]] == [cisco["id"]]

    def test_trim_repeated_selected_items_parses_recommendation_form(self):
        from bcn.services.writer.service import WriterService

        settings = _make_settings()
        service = WriterService(settings)
        cisco = {
            "id": str(uuid4()),
            "title": "CVE-2026-20127 (Cisco SD-WAN, CVSS 10) has been actively exploited since 2023",
            "summary": "PatchIntel breakdown of the active exploitation campaign.",
            "url": "https://patchintel.substack.com/p/patchintel-issue-1-week-of-march?r=7v8ayn",
        }
        budibase = {
            "id": str(uuid4()),
            "title": "@budibase/server: Command Injection in PostgreSQL Dump Command",
            "summary": "GHSA-726g-59wr-cj4c impacts backup export handling.",
            "url": "https://github.com/advisories/GHSA-726g-59wr-cj4c",
        }
        history = [
            {
                "content_markdown": (
                    "[Cisco flags more SD-WAN flaws as actively exploited in attacks]"
                    "(https://www.bleepingcomputer.com/news/security/cisco-flags-more-sd-wan-flaws-as-actively-exploited-in-attacks/)"
                )
            }
        ]
        critique = {
            "issues": [],
            "recommendations": [
                "Drop the Cisco SD-WAN segment; we already covered these actively exploited flaws in briefing 5 via BleepingComputer.",
            ],
        }

        trimmed = service.trim_repeated_selected_items(
            selected_items=[cisco, budibase],
            critique=critique,
            history=history,
        )

        assert [item["id"] for item in trimmed["selected_items"]] == [budibase["id"]]
        assert [item["id"] for item in trimmed["dropped_items"]] == [cisco["id"]]

    def test_extract_sticky_rewrite_constraints_from_review_findings(self):
        from bcn.services.writer.service import WriterService

        service = WriterService(_make_settings())
        critique = {
            "issues": [],
            "recommendations": [
                "Drop the 'Welcome to another week' boilerplate. Start directly with the punchy summary.",
            ],
        }
        verifier = {
            "issues": [
                "Factual overreach: The draft claims active attacker use, but the source only proves capability.",
                "Assumption: The draft specifies a semicolon payload detail that is not provided in the source summary.",
            ]
        }

        constraints = service.extract_sticky_rewrite_constraints(critique, verifier)

        assert any("active exploitation" in item for item in constraints)
        assert any("payload mechanics" in item for item in constraints)
        assert any("opener boilerplate" in item for item in constraints)

    def test_build_rewrite_feedback_context_includes_sticky_constraints(self):
        from bcn.services.writer.service import WriterService

        service = WriterService(_make_settings())
        context = service.build_rewrite_feedback_context(
            gate={"passed": True, "issues": [], "hard_issues": [], "soft_issues": []},
            critique={
                "passed": True,
                "score": 90,
                "issues": [],
                "recommendations": [],
                "dimension_scores": {
                    "actionability": 90,
                    "source_diversity": 90,
                    "link_hygiene": 90,
                    "clarity": 90,
                    "style": 90,
                },
            },
            verification={"passed": True, "score": 95, "issues": [], "recommendations": []},
            mode="standard",
            min_chars=800,
            target_chars=1100,
            hard_max_chars=1500,
            rewrite_attempt=2,
            max_rewrites=7,
            selected_items=[],
            sticky_constraints=[
                "Do not claim active exploitation unless the source explicitly says it.",
            ],
        )

        assert context["sticky_constraints"] == [
            "Do not claim active exploitation unless the source explicitly says it.",
        ]
        assert "score" not in context["critic"]
        assert context["critic"]["threshold_failures"] == {}

    def test_build_rewrite_feedback_context_exposes_structured_threshold_failures(self):
        from bcn.services.writer.service import WriterService

        service = WriterService(_make_settings())
        context = service.build_rewrite_feedback_context(
            gate={"passed": True, "issues": [], "hard_issues": [], "soft_issues": []},
            critique={
                "passed": True,
                "score": 96,
                "issues": [],
                "recommendations": [],
                "dimension_scores": {
                    "actionability": 60,
                    "source_diversity": 90,
                    "link_hygiene": 92,
                    "clarity": 90,
                    "style": 90,
                },
                "threshold_failures": {
                    "actionability": {"actual": 60, "required": 70}
                },
            },
            verification={"passed": True, "score": 95, "issues": [], "recommendations": []},
            mode="standard",
            min_chars=800,
            target_chars=1100,
            hard_max_chars=1500,
            rewrite_attempt=2,
            max_rewrites=7,
            selected_items=[],
        )

        assert context["critic"]["threshold_failures"] == {
            "actionability": {"actual": 60, "required": 70}
        }
        assert context["critic"]["failed_thresholds"] == ["actionability"]
        assert any(
            "actionability" in item for item in context["priority_order"]
        )

    def test_normalize_review_payloads_accept_service_shapes(self):
        from bcn.services.writer.review import normalize_critique_payload
        from bcn.services.writer.review import normalize_verifier_payload

        critique = normalize_critique_payload(
            {
                "critic_passed": True,
                "critic_score": 91,
                "critic_dimension_scores": {"actionability": 88, "link_hygiene": 90},
                "critic_issues": ["issue a"],
                "recommendations": ["rec a"],
            }
        )
        verifier = normalize_verifier_payload(
            {
                "verifier_passed": True,
                "verifier_score": 95,
                "issues": ["issue b"],
                "recommendations": ["rec b"],
            }
        )

        assert critique["passed"] is True
        assert critique["score"] == 91
        assert critique["dimension_scores"] == {
            "actionability": 88,
            "link_hygiene": 90,
        }
        assert critique["threshold_failures"] == {}
        assert critique["issues"] == ["issue a"]
        assert verifier["passed"] is True
        assert verifier["score"] == 95
        assert verifier["issues"] == ["issue b"]

    @pytest.mark.asyncio
    async def test_generate_release_candidate_redrafts_after_dropping_repeated_items(self):
        settings = _make_settings(briefing_critique_max_rounds=2)
        service = WriterService(settings)
        pingora = {
            "id": "pingora",
            "title": "Fixing request smuggling vulnerabilities in Pingora OSS deployments",
            "summary": "Cloudflare patched request smuggling in Pingora OSS.",
            "url": "https://blog.cloudflare.com/pingora-oss-smuggling-vulnerabilities/",
        }
        cisco = {
            "id": "cisco",
            "title": "CVE-2026-20127 (Cisco SD-WAN, CVSS 10) has been actively exploited since 2023",
            "summary": "PatchIntel breakdown of the active exploitation campaign.",
            "url": "https://patchintel.substack.com/p/patchintel-issue-1-week-of-march?r=7v8ayn",
        }
        budibase = {
            "id": "budibase",
            "title": "@budibase/server: Command Injection in PostgreSQL Dump Command",
            "summary": "GHSA-726g-59wr-cj4c impacts backup export handling.",
            "url": "https://github.com/advisories/GHSA-726g-59wr-cj4c",
        }
        history = [
            {
                "content_markdown": (
                    "[Pingora vulnerable to HTTP Request Smuggling via Premature Upgrade]"
                    "(https://github.com/cloudflare/pingora/security/advisories/GHSA-xq2h-p299-vjwv)\n"
                    "[Cisco flags more SD-WAN flaws as actively exploited in attacks]"
                    "(https://www.bleepingcomputer.com/news/security/cisco-flags-more-sd-wan-flaws-as-actively-exploited-in-attacks/)"
                )
            }
        ]

        initial_draft = "Initial draft"
        trimmed_draft = "Trimmed draft"
        with (
            patch.object(
                service.writer_llm,
                "generate_briefing",
                new_callable=AsyncMock,
                side_effect=[initial_draft, trimmed_draft],
            ) as mock_generate,
            patch.object(
                service,
                "postprocess_briefing",
                new_callable=AsyncMock,
                side_effect=[
                    PostprocessedBriefing(
                        markdown=initial_draft,
                        selected_items=[pingora, cisco, budibase],
                    ),
                    PostprocessedBriefing(
                        markdown=trimmed_draft,
                        selected_items=[budibase],
                    ),
                ],
            ),
            patch.object(
                service,
                "evaluate_existing_markdown",
                new_callable=AsyncMock,
                side_effect=[
                    {
                        "markdown": initial_draft,
                        "mode": "standard",
                        "min_chars": 10,
                        "target_chars": 100,
                        "hard_max_chars": 1000,
                        "gate": {"passed": True, "issues": []},
                        "critique": {
                            "passed": True,
                            "score": 40,
                            "issues": [
                                "Repeated topic: Pingora HTTP request smuggling (already covered in briefing 4)",
                                "Repeated topic: Cisco SD-WAN active exploitation (already covered in briefing 5)",
                            ],
                            "recommendations": [],
                        },
                        "critic_threshold_passed": False,
                        "verifier": {"passed": True, "issues": [], "recommendations": []},
                        "release_passed": False,
                    },
                    {
                        "markdown": trimmed_draft,
                        "mode": "standard",
                        "min_chars": 10,
                        "target_chars": 100,
                        "hard_max_chars": 1000,
                        "gate": {"passed": True, "issues": []},
                        "critique": {
                            "passed": True,
                            "score": 95,
                            "issues": [],
                            "recommendations": [],
                            "dimension_scores": {
                                "actionability": 90,
                                "link_hygiene": 90,
                            },
                        },
                        "critic_threshold_passed": True,
                        "verifier": {"passed": True, "issues": [], "recommendations": []},
                        "release_passed": True,
                    },
                ],
            ),
        ):
            candidate = await service.generate_release_candidate(
                selected_items=[pingora, cisco, budibase],
                history=history,
                mode="standard",
            )

        assert candidate["release_passed"] is True
        assert candidate["rewrites"] == 1
        assert [item["id"] for item in candidate["selected_items"]] == ["budibase"]
        assert mock_generate.await_count == 2
        assert [item["id"] for item in mock_generate.await_args_list[1].args[0]] == [
            "budibase"
        ]

    def test_dedupe_markdown_links_uses_canonical_url_key(self):
        from bcn.services.writer.service import WriterService

        settings = _make_settings()
        service = WriterService(settings)
        markdown = (
            "[Primary](https://www.example.com/path/?b=1&utm_source=digest&fbclid=abc)\n"
            "[Duplicate](https://example.com/path?b=1)\n"
            "[Other](https://example.com/path?b=2)"
        )

        deduped = service.dedupe_markdown_links(markdown)
        assert (
            "[Primary](https://www.example.com/path/?b=1&utm_source=digest&fbclid=abc)"
            in deduped
        )
        assert "[Duplicate](" not in deduped
        assert "Duplicate" in deduped
        assert "[Other](https://example.com/path?b=2)" in deduped

    def test_social_proof_bonus_prioritizes_high_engagement_tweet(self):
        from bcn.services.writer.service import WriterService

        settings = _make_settings(
            briefing_social_proof_weight=0.35,
            briefing_social_proof_max_bonus=2.5,
        )
        service = WriterService(settings)

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

        assert service.priority_score(high_tweet) > service.priority_score(
            low_reddit
        )

    def test_source_floor_filters_low_social_noise(self):
        from bcn.services.writer.service import WriterService

        settings = _make_settings(
            briefing_min_reddit_engagement_score=40,
            briefing_min_twitter_engagement_score=400,
            briefing_social_floor_exempt_relevance=9,
        )
        service = WriterService(settings)

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

        assert service.passes_source_floor(low_reddit) is False
        assert service.passes_source_floor(high_tweet) is True
        assert service.passes_source_floor(exempt_high_relevance) is True

    def test_quality_gate_flags_missing_urls_and_structure(self):
        from bcn.services.writer.service import WriterService

        settings = _make_settings()
        service = WriterService(settings)
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

        gate = service.quality_gate(
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
        from bcn.services.writer.service import WriterService

        settings = _make_settings(briefing_gate_mode="balanced")
        service = WriterService(settings)
        selected = [{"url": "https://example.com/one", "source_type": "rss"}]
        markdown = "**Quick Signal**\n[One](https://example.com/one) patch now."

        gate = service.quality_gate(
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
        from bcn.services.writer.service import WriterService

        settings = _make_settings(briefing_gate_mode="strict")
        service = WriterService(settings)
        selected = [
            {"url": "https://example.com/one", "source_type": "rss"},
            {"url": "https://example.com/two", "source_type": "ghsa"},
        ]
        markdown = "**Quick Signal**\n[One](https://example.com/one) patch now."

        gate = service.quality_gate(
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
        from bcn.services.writer.service import WriterService

        settings = _make_settings()
        service = WriterService(settings)
        markdown = (
            "**Detection: AI Security at the Edge**\n"
            "Cloudflare blocks unsafe prompts early.\n"
            "*Source: [Cloudflare Blog](https://blog.cloudflare.com/block-unsafe-llm-prompts-with-firewall-for-ai/)*"
        )

        rewritten = service.de_template_fields(markdown)
        assert "**AI Security at the Edge**" in rewritten
        assert "Detection:" not in rewritten
        assert "Source:" not in rewritten
        assert "reference: [Cloudflare Blog]" in rewritten

    def test_missing_items_fallback_is_readable_without_fixed_heading(self):
        from bcn.services.writer.service import WriterService

        settings = _make_settings()
        service = WriterService(settings)
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

        out = service.append_missing_items_section(markdown, missing)
        assert "Additional High-Signal Items" not in out
        assert "[First extra](https://example.com/one)" in out
        assert "[Second extra](https://example.com/two)" in out
        assert "• [Second extra](https://example.com/two)" in out

    def test_strip_unselected_github_advisory_links_keeps_selected_only(self):
        from bcn.services.writer.service import WriterService

        settings = _make_settings()
        service = WriterService(settings)

        selected = [
            {"url": "https://github.com/advisories/GHSA-w6x6-9fp7-fqm4"},
        ]
        markdown = (
            "Bad ref [GHSA-78q6-223p-8x4q](https://github.com/advisories/GHSA-78q6-223p-8x4q)\n\n"
            "Also bad [GHSA-9c6g-9j6q-6w4w](https://github.com/craftcms/cms/security/advisories/GHSA-9c6g-9j6q-6w4w)\n\n"
            "Good ref [GHSA-w6x6-9fp7-fqm4](https://github.com/advisories/GHSA-w6x6-9fp7-fqm4)"
        )

        out = service.strip_unselected_github_advisory_links(markdown, selected)
        assert "https://github.com/advisories/GHSA-78q6-223p-8x4q" not in out
        assert (
            "https://github.com/craftcms/cms/security/advisories/GHSA-9c6g-9j6q-6w4w"
            not in out
        )
        assert "GHSA-78q6-223p-8x4q" in out
        assert "GHSA-9c6g-9j6q-6w4w" in out
        assert "https://github.com/advisories/GHSA-w6x6-9fp7-fqm4" in out

    def test_strip_unselected_markdown_links_keeps_only_selected_urls(self):
        from bcn.services.writer.service import WriterService

        settings = _make_settings()
        service = WriterService(settings)

        selected = [
            {"url": "https://example.com/selected"},
        ]
        markdown = (
            "Keep [Selected](https://example.com/selected)\n\n"
            "Drop [Repeat](https://blog.calif.io/p/a-race-within-a-race-exploiting-cve)"
        )

        out = service.strip_unselected_markdown_links(markdown, selected)
        assert "[Selected](https://example.com/selected)" in out
        assert "https://blog.calif.io/p/a-race-within-a-race-exploiting-cve" not in out
        assert "Drop Repeat" in out

    def test_quiet_day_mode_detection(self):
        from bcn.services.writer.service import WriterService

        settings = _make_settings(
            briefing_quiet_day_enabled=True,
            briefing_quiet_day_high_signal_threshold=8,
            briefing_quiet_day_min_high_signal_items=3,
        )
        service = WriterService(settings)

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

        assert service.is_quiet_day(low_signal_items) is True
        assert service.is_quiet_day(high_signal_items) is False

    def test_single_item_char_limits_are_relaxed(self):
        from bcn.services.writer.service import WriterService

        settings = _make_settings(
            briefing_min_chars=1200,
            briefing_target_chars=1700,
            briefing_hard_max_chars=2300,
            briefing_single_item_min_chars=420,
            briefing_single_item_target_chars=760,
            briefing_single_item_hard_max_chars=1200,
        )
        service = WriterService(settings)

        min_chars, target_chars, hard_max_chars = service.char_limits(
            "standard",
            selected_count=1,
        )
        assert min_chars == 420
        assert target_chars == 760
        assert hard_max_chars == 1200

    @pytest.mark.asyncio
    async def test_postprocess_drop_recomputes_missing_urls_before_enrich(self):
        from bcn.services.writer.service import WriterService

        settings = _make_settings(
            briefing_missing_coverage_max_drops=1,
            briefing_min_items_after_coverage_drop=1,
        )
        service = WriterService(settings)
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
                service.writer_llm,
                "enrich_briefing",
                new_callable=AsyncMock,
                return_value=draft,
            ) as mock_enrich,
            patch.object(
                service,
                "priority_score",
                side_effect=lambda item: 0 if item["id"] == "two" else 1,
            ),
        ):
            out = await service.postprocess_briefing(
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
        from bcn.services.writer.service import WriterService

        settings = _make_settings(
            briefing_missing_coverage_max_drops=0,
        )
        service = WriterService(settings)
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
            service.writer_llm,
            "enrich_briefing",
            new_callable=AsyncMock,
            return_value=draft,
        ) as mock_enrich:
            out = await service.postprocess_briefing(
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
        from bcn.services.writer.service import WriterService

        settings = _make_settings(
            briefing_missing_coverage_max_drops=0,
        )
        service = WriterService(settings)
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
            service.writer_llm,
            "enrich_briefing",
            new_callable=AsyncMock,
            return_value=draft,
        ):
            out = await service.postprocess_briefing(
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
        from bcn.services.writer.service import WriterService

        settings = _make_settings()
        service = WriterService(settings)

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

        assert service.passes_critic_thresholds(critique) is False

    def test_passes_critic_thresholds_does_not_block_low_source_diversity(self):
        from bcn.services.writer.service import WriterService

        settings = _make_settings()
        service = WriterService(settings)

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

        assert service.passes_critic_thresholds(critique) is True

# ── Distributor tests ────────────────────────────────────────────────────


class TestDistributorService:
    def test_build_channels_daily_mode_only_telegram_and_discord(self):
        from bcn.services.distributor.service import DistributorService

        settings = _make_settings(
            telegram_bot_token="123:abc",
            telegram_chat_id="@broken-cloud",
            discord_bot_token="discord-token",
            discord_channel_id="12345",
            ghost_admin_api_key="ghost-id:" + ("1f" * 32),
            ghost_admin_api_url="https://brokencloudnews.ghost.io",
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

    def test_build_channels_daily_mode_includes_ghost_when_enabled(self):
        from bcn.services.distributor.service import DistributorService

        settings = _make_settings(
            telegram_bot_token="123:abc",
            telegram_chat_id="@broken-cloud",
            discord_bot_token="discord-token",
            discord_channel_id="12345",
            ghost_enabled=True,
            ghost_admin_api_key="ghost-id:" + ("1f" * 32),
            ghost_admin_api_url="https://brokencloudnews.ghost.io",
        )
        service = DistributorService(settings)
        channels = service._build_channels(mode="regular_daily_briefing")
        names = [name for name, _channel in channels]
        assert names == ["telegram", "discord", "ghost"]

    def test_build_channels_monthly_mode_email_only(self):
        from bcn.services.distributor.service import DistributorService

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
        from bcn.services.distributor.service import DistributorService

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
        from bcn.services.distributor.service import DeliveryRequest
        from bcn.services.distributor.service import DistributorService

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
        from bcn.services.distributor.service import DeliveryRequest
        from bcn.services.distributor.service import DistributorService

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
        from bcn.services.distributor.service import DeliveryRequest
        from bcn.services.distributor.service import DistributorService

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
