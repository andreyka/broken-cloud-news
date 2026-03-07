from __future__ import annotations

from pydantic import ValidationError
import pytest

from bcn.common.config import Settings


class TestSettings:
    def test_defaults(self):
        s = Settings()
        assert s.llm_timeout == 180
        assert s.llm_chat_retries == 16
        assert s.llm_retry_max_wait_seconds == 600
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

    def test_distribute_hours_csv_parsing(self, monkeypatch):
        monkeypatch.setenv("BCN_DISTRIBUTE_HOURS", "9,13,19")
        s = Settings()
        assert s.distribute_hours == [9, 13, 19]

    def test_distribute_hours_json_parsing_and_dedup(self, monkeypatch):
        monkeypatch.setenv("BCN_DISTRIBUTE_HOURS", "[19, 9, 13, 9]")
        s = Settings()
        assert s.distribute_hours == [19, 9, 13]

    def test_distribute_hours_invalid_range(self, monkeypatch):
        monkeypatch.setenv("BCN_DISTRIBUTE_HOURS", "9,25")
        with pytest.raises(ValidationError):
            Settings()

    def test_distribute_timezone_validation(self):
        assert Settings(distribute_timezone="America/Los_Angeles").distribute_timezone == (
            "America/Los_Angeles"
        )
        with pytest.raises(ValidationError):
            Settings(distribute_timezone="Mars/Olympus_Mons")

    def test_monthly_newsletter_day_validation(self):
        assert Settings(monthly_newsletter_day=1).monthly_newsletter_day == 1
        with pytest.raises(ValidationError):
            Settings(monthly_newsletter_day=31)

    def test_shadow_minutes_before_publish_validation(self):
        assert Settings(shadow_minutes_before_publish=45).shadow_minutes_before_publish == 45
        with pytest.raises(ValidationError):
            Settings(shadow_minutes_before_publish=1440)
