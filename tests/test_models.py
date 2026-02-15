from __future__ import annotations

import pytest
from uuid import uuid4
from datetime import datetime, timezone

from bcn.models import AnalysisResult, NewsItem, Briefing


class TestAnalysisResult:
    def test_valid(self):
        r = AnalysisResult(
            summary="A critical vuln",
            relevance_score=8,
            tags=["k8s", "cloud"],
            image_prompt="cyberpunk server",
        )
        assert r.relevance_score == 8
        assert r.tags == ["k8s", "cloud"]

    def test_score_bounds(self):
        r = AnalysisResult(summary="ok", relevance_score=1)
        assert r.relevance_score == 1
        r = AnalysisResult(summary="ok", relevance_score=10)
        assert r.relevance_score == 10

    def test_score_out_of_range(self):
        with pytest.raises(Exception):
            AnalysisResult(summary="bad", relevance_score=0)
        with pytest.raises(Exception):
            AnalysisResult(summary="bad", relevance_score=11)

    def test_defaults(self):
        r = AnalysisResult(summary="s", relevance_score=5)
        assert r.tags == []
        assert r.image_prompt == "cloud security concept art"


class TestNewsItem:
    def test_minimal(self):
        item = NewsItem(
            id=uuid4(),
            source_type="ghsa",
            source_id="GHSA-xxxx",
            url="https://example.com",
            published_at=datetime.now(timezone.utc),
        )
        assert item.status == "NEW"
        assert item.summary is None

    def test_full(self):
        item = NewsItem(
            id=uuid4(),
            source_type="rss",
            source_id="rss-1",
            url="https://example.com/post",
            title="Test",
            published_at=datetime.now(timezone.utc),
            summary="summary",
            relevance_score=7,
            ai_tags=["cloud"],
            status="ANALYZED",
        )
        assert item.relevance_score == 7
        assert item.ai_tags == ["cloud"]


class TestBriefing:
    def test_defaults(self):
        b = Briefing(
            id=uuid4(),
            created_at=datetime.now(timezone.utc),
            content_markdown="# Test",
        )
        assert b.status == "DRAFT"
        assert b.item_ids == []
        assert b.cover_image_url is None
