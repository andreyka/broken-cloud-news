from __future__ import annotations

from pydantic import ValidationError
import pytest

from bcn.common.component_settings import CollectorServiceSettings
from bcn.common.component_settings import DistributorServiceSettings
from bcn.common.component_settings import load_component_service_settings
from bcn.common.component_settings import WriterServiceSettings
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
        assert s.ghsa_interval_hours == 4
        assert s.briefing_history_items == 10
        assert s.briefing_novelty_lookback_hours == 24 * 21
        assert s.briefing_novelty_title_similarity_threshold == 0.78
        assert s.service_request_timeout_seconds == 900
        assert s.writer_service_url == ""
        assert s.distributor_service_url == ""
        assert s.writer_prompt_bundle_path == ""
        assert s.critic_prompt_path == ""
        assert s.verifier_prompt_path == ""

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("BCN_LLM_TIMEOUT", "60")
        monkeypatch.setenv("BCN_GHSA_INTERVAL_HOURS", "6")
        monkeypatch.setenv("BCN_LLM_PROVIDER", "gemini")
        monkeypatch.setenv("BCN_LLM_PROVIDER_CRITIC", "openai")
        s = Settings()
        assert s.llm_timeout == 60
        assert s.llm_provider == "gemini"
        assert s.llm_provider_critic == "openai_compat"
        assert s.ghsa_interval_hours == 6

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

    def test_service_url_validation_and_normalization(self):
        settings = Settings(writer_service_url="http://writer.internal:8081/")
        assert settings.writer_service_url == "http://writer.internal:8081"
        distributor_settings = Settings(
            distributor_service_url="http://distributor.internal:8086/"
        )
        assert (
            distributor_settings.distributor_service_url
            == "http://distributor.internal:8086"
        )
        with pytest.raises(ValidationError):
            Settings(critic_service_url="writer.internal:8082")

    def test_service_request_timeout_must_be_positive(self):
        with pytest.raises(ValidationError):
            Settings(service_request_timeout_seconds=0)

    def test_component_settings_loader_returns_narrow_service_model(self, monkeypatch):
        monkeypatch.setenv("BCN_SERVICE_AUTH_TOKEN", "shared-token")
        monkeypatch.setenv("BCN_RSS_FEEDS", "[\"https://example.com/feed.xml\"]")
        settings = load_component_service_settings("collector")

        assert isinstance(settings, CollectorServiceSettings)
        assert settings.service_auth_token == "shared-token"
        assert settings.rss_feeds == ["https://example.com/feed.xml"]
        assert not hasattr(settings, "writer_service_url")

    def test_writer_component_settings_do_not_expose_control_plane_db(self):
        settings = load_component_service_settings("writer")

        assert isinstance(settings, WriterServiceSettings)
        assert not hasattr(settings, "database_url")

    def test_distributor_component_settings_use_trusted_image_sources(self, monkeypatch):
        monkeypatch.setenv(
            "BCN_TRUSTED_IMAGE_SOURCE_URLS",
            "[\"https://images.internal\", \"https://cdn.internal\"]",
        )
        settings = load_component_service_settings("distributor")

        assert isinstance(settings, DistributorServiceSettings)
        assert settings.trusted_image_source_urls == [
            "https://images.internal",
            "https://cdn.internal",
        ]
        assert not hasattr(settings, "comfyui_url")
