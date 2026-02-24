from __future__ import annotations

import os

from bcn.config import Settings


class TestSettings:
    def test_defaults(self):
        s = Settings()
        assert s.llm_timeout == 120
        assert s.llm_provider == "openai_compat"
        assert s.llm_model_writer == ""
        assert s.ghsa_severities == ["CRITICAL", "HIGH"]
        assert s.collector_port == 9001
        assert s.briefing_history_items == 10
        assert s.briefing_novelty_lookback_hours == 24 * 21
        assert s.briefing_novelty_title_similarity_threshold == 0.78

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("BCN_LLM_TIMEOUT", "60")
        monkeypatch.setenv("BCN_COLLECTOR_PORT", "5000")
        monkeypatch.setenv("BCN_LLM_PROVIDER", "gemini")
        monkeypatch.setenv("BCN_LLM_PROVIDER_CRITIC", "openai")
        s = Settings()
        assert s.llm_timeout == 60
        assert s.llm_provider == "gemini"
        assert s.llm_provider_critic == "openai_compat"
        assert s.collector_port == 5000

    def test_vertex_provider_aliases(self, monkeypatch):
        monkeypatch.setenv("BCN_LLM_PROVIDER", "vertex")
        monkeypatch.setenv("BCN_LLM_PROVIDER_WRITER", "vertex_ai")
        s = Settings()
        assert s.llm_provider == "vertexai"
        assert s.llm_provider_writer == "vertexai"

    def test_empty_string_list_fields(self, monkeypatch):
        """Empty env strings should fall back to default lists, not become ['']."""
        monkeypatch.setenv("BCN_TWITTER_HANDLES", "")
        monkeypatch.setenv("BCN_TWITTER_REQUIRED_KEYWORDS", "")
        monkeypatch.setenv("BCN_RSS_FEEDS", "")
        monkeypatch.setenv("BCN_REDDIT_SUBREDDITS", "")
        monkeypatch.setenv("BCN_GHSA_KEYWORDS", "")
        s = Settings()
        assert s.twitter_handles == []
        assert s.twitter_required_keywords == []
        assert s.rss_feeds == []
        assert s.reddit_subreddits == []
        assert s.ghsa_keywords == []
