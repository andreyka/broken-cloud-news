"""Application settings loaded from environment variables."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError

from pydantic import field_validator
from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict


class Settings(BaseSettings):
    """BCN configuration backed by ``BCN_``-prefixed environment variables."""

    _AGENT_PORT_FIELDS = {
        "collector": "collector_port",
        "analyst": "analyst_port",
        "writer": "writer_port",
        "distributor": "distributor_port",
        "critic": "critic_port",
        "verifier": "verifier_port",
    }
    _AGENT_URL_FIELDS = {
        "collector": "collector_agent_url",
        "analyst": "analyst_agent_url",
        "writer": "writer_agent_url",
        "distributor": "distributor_agent_url",
        "critic": "critic_agent_url",
        "verifier": "verifier_agent_url",
    }

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="BCN_",
        enable_decoding=False,
    )

    @field_validator(
        "twitter_handles",
        "rss_feeds",
        "ghsa_severities",
        "ghsa_keywords",
        "email_recipients",
        "reddit_subreddits",
        "twitter_required_keywords",
        "distribute_hours",
        mode="before",
    )
    @classmethod
    def _empty_str_to_list(cls, v: Any) -> Any:
        """Allow empty env-var strings to fall back to the field default."""
        if isinstance(v, str) and v.strip() == "":
            return []
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                pass
        return v

    @field_validator("distribute_hours", mode="before")
    @classmethod
    def _parse_distribute_hours(cls, v: Any) -> Any:
        """Parse BCN_DISTRIBUTE_HOURS from CSV or JSON list forms."""
        if v is None:
            return []
        if isinstance(v, str):
            raw = v.strip()
            if not raw:
                return []
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return parsed
            if parsed is not None:
                return [parsed]
            return [token.strip() for token in raw.split(",") if token.strip()]
        if isinstance(v, (int, float)):
            return [int(v)]
        return v

    @field_validator("distribute_hours")
    @classmethod
    def _validate_distribute_hours(cls, v: list[Any]) -> list[int]:
        """Ensure schedule hours are valid 0..23 values and deduplicated."""
        normalized: list[int] = []
        seen: set[int] = set()
        for raw in v or []:
            try:
                hour = int(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Invalid distribute hour '{raw}'; expected integer 0..23"
                ) from exc
            if hour < 0 or hour > 23:
                raise ValueError(
                    f"Invalid distribute hour '{hour}'; expected integer 0..23"
                )
            if hour not in seen:
                normalized.append(hour)
                seen.add(hour)
        return normalized

    @field_validator("distribute_timezone", "monthly_newsletter_timezone")
    @classmethod
    def _validate_schedule_timezone(cls, v: str) -> str:
        """Validate IANA timezone used by cron scheduling."""
        value = (v or "").strip() or "UTC"
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Invalid timezone '{value}'") from exc
        return value

    @field_validator("monthly_newsletter_day")
    @classmethod
    def _validate_monthly_day(cls, v: int) -> int:
        day = int(v)
        if day < 1 or day > 28:
            raise ValueError("monthly_newsletter_day must be between 1 and 28")
        return day

    @field_validator("monthly_newsletter_hour")
    @classmethod
    def _validate_monthly_hour(cls, v: int) -> int:
        hour = int(v)
        if hour < 0 or hour > 23:
            raise ValueError("monthly_newsletter_hour must be between 0 and 23")
        return hour

    @field_validator("monthly_newsletter_minute")
    @classmethod
    def _validate_monthly_minute(cls, v: int) -> int:
        minute = int(v)
        if minute < 0 or minute > 59:
            raise ValueError("monthly_newsletter_minute must be between 0 and 59")
        return minute

    @field_validator("shadow_minutes_before_publish")
    @classmethod
    def _validate_shadow_minutes_before_publish(cls, v: int) -> int:
        minutes = int(v)
        if minutes < 0 or minutes >= 24 * 60:
            raise ValueError("shadow_minutes_before_publish must be between 0 and 1439")
        return minutes

    @field_validator("telegram_overflow_mode")
    @classmethod
    def _validate_telegram_overflow_mode(cls, v: str) -> str:
        mode = (v or "").strip().lower()
        return mode if mode in {"smart", "always", "never"} else "smart"

    @field_validator(
        "collector_agent_url",
        "analyst_agent_url",
        "writer_agent_url",
        "distributor_agent_url",
        "critic_agent_url",
        "verifier_agent_url",
    )
    @classmethod
    def _validate_agent_url(cls, v: str) -> str:
        value = str(v or "").strip()
        if not value:
            return ""
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(
                f"Invalid agent URL '{value}'; expected absolute http(s) URL"
            )
        return value.rstrip("/")

    @field_validator("briefing_gate_mode")
    @classmethod
    def _validate_briefing_gate_mode(cls, v: str) -> str:
        mode = (v or "").strip().lower()
        return mode if mode in {"strict", "balanced", "minimal"} else "balanced"

    @field_validator("llm_provider")
    @classmethod
    def _validate_llm_provider(cls, v: str) -> str:
        value = (v or "").strip().lower()
        aliases = {
            "openai": "openai_compat",
            "openai_compat": "openai_compat",
            "openai-compatible": "openai_compat",
            "gemini": "gemini",
            "gemini_native": "gemini",
            "google": "gemini",
            "vertexai": "vertexai",
            "vertex_ai": "vertexai",
            "vertex": "vertexai",
            "google_vertex": "vertexai",
        }
        if not value:
            return "openai_compat"
        return aliases.get(value, "openai_compat")

    @field_validator(
        "llm_provider_analyst",
        "llm_provider_writer",
        "llm_provider_critic",
        "llm_provider_verifier",
        "llm_provider_cover",
    )
    @classmethod
    def _validate_llm_role_provider(cls, v: str) -> str:
        value = (v or "").strip().lower()
        aliases = {
            "openai": "openai_compat",
            "openai_compat": "openai_compat",
            "openai-compatible": "openai_compat",
            "gemini": "gemini",
            "gemini_native": "gemini",
            "google": "gemini",
            "vertexai": "vertexai",
            "vertex_ai": "vertexai",
            "vertex": "vertexai",
            "google_vertex": "vertexai",
        }
        if not value:
            return ""
        return aliases.get(value, "openai_compat")

    # Database
    database_url: str = "postgresql://broken_cloud_news_agent_db:cloud_security_agent@localhost:5432/broken_cloud_news"

    # LLM
    llm_provider: str = "openai_compat"  # openai_compat, gemini, vertexai
    llm_base_url: str = "http://192.168.0.9:8000/v1"
    llm_model: str = "Qwen/Qwen3-VL-30B-A3B-Instruct-FP8"
    llm_api_key: str = ""
    llm_timeout: int = 180
    llm_chat_retries: int = 16
    llm_retry_max_wait_seconds: int = 600
    llm_retry_jitter_min_seconds: float = 0.5
    llm_retry_jitter_max_seconds: float = 5.0

    # LLM role overrides (optional; empty string => fall back to shared setting)
    llm_provider_analyst: str = ""
    llm_provider_writer: str = ""
    llm_provider_critic: str = ""
    llm_provider_verifier: str = ""
    llm_provider_cover: str = ""
    llm_base_url_analyst: str = ""
    llm_base_url_writer: str = ""
    llm_base_url_critic: str = ""
    llm_base_url_verifier: str = ""
    llm_base_url_cover: str = ""
    llm_model_analyst: str = ""
    llm_model_writer: str = ""
    llm_model_critic: str = ""
    llm_model_verifier: str = ""
    llm_model_cover: str = ""
    llm_api_key_analyst: str = ""
    llm_api_key_writer: str = ""
    llm_api_key_critic: str = ""
    llm_api_key_verifier: str = ""
    llm_api_key_cover: str = ""

    # ComfyUI (Flux on DGX Spark)
    comfyui_url: str = "http://192.168.0.9:8188"
    comfyui_timeout: int = 300
    comfyui_poll_interval: int = 2

    # GitHub
    github_token: str = ""

    # X API (Twitter)
    twitter_bearer_token: str = ""
    twitter_handles: list[str] = [
        "GoogleVRP",
        "anton_chuvakin",
        "AISecHub",
        "_JohnHammond",
        "lauriewired",
        "chompie1337",
        "Steph3nSims",
        "KimZetter",
        "argvee",
        "avkovaleff",
        "tom_doerr",
        "gadievron",
        "Dinosn",
        "steipete",
        "philvenables",
        "Fox0x01",
        "Laughing_Mantis",
        "virusbtn",
        "thegrugq",
        "lukOlejnik",
    ]
    twitter_max_items: int = 20
    twitter_required_keywords: list[str] = [
        "cloud",
        "aws",
        "azure",
        "gcp",
        "kubernetes",
        "k8s",
        "container",
        "docker",
        "terraform",
        "iam",
        "cve",
        "vuln",
        "exploit",
        "rce",
        "advisory",
        "serverless",
        "cloudflare",
        "envoy",
        "qemu",
        "kvm",
        "postgres",
        "clickhouse",
        "redis",
        "load balancer",
    ]

    # RSS feeds
    rss_feeds: list[str] = [
        "https://www.cisa.gov/cybersecurity-advisories/all.xml",
        "https://aws.amazon.com/blogs/security/feed/",
        "https://blog.cloudflare.com/tag/security/rss/",
        "https://unit42.paloaltonetworks.com/feed/",
        "https://research.checkpoint.com/feed/",
        "https://www.wiz.io/feed/rss.xml",
    ]

    # Reddit (RSS)
    reddit_subreddits: list[str] = [
        "cloudsecuritypros",
        "cybersecurity",
        "awssecurity",
        "azure",
        "googlecloud",
        "kubernetes",
        "netsec",
        "terraform",
    ]
    reddit_max_items_per_subreddit: int = 15

    # GHSA filter
    ghsa_severities: list[str] = ["CRITICAL", "HIGH"]
    ghsa_keywords: list[str] = [
        "kubernetes",
        "k8s",
        "docker",
        "container",
        "aws",
        "azure",
        "gcp",
        "cloud",
        "terraform",
        "iam",
        "envoy",
        "qemu",
        "kvm",
        "postgres",
        "clickhouse",
        "redis",
        "cloudflare",
        "load balancer",
    ]

    # Distribution: Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Distribution: Email
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    email_from: str = ""
    email_recipients: list[str] = []

    # Distribution: Slack
    slack_webhook_url: str = ""

    # Distribution: Discord
    discord_bot_token: str = ""
    discord_channel_id: str = ""

    # Agent ports
    collector_port: int = 9001
    analyst_port: int = 9002
    writer_port: int = 9003
    distributor_port: int = 9004
    critic_port: int = 9005
    verifier_port: int = 9006
    collector_agent_url: str = ""
    analyst_agent_url: str = ""
    writer_agent_url: str = ""
    distributor_agent_url: str = ""
    critic_agent_url: str = ""
    verifier_agent_url: str = ""

    # Scheduling
    ghsa_interval_hours: int = 4
    rss_interval_hours: int = 2
    reddit_interval_hours: int = 3
    twitter_interval_hours: int = 6
    analyst_interval_minutes: int = 15
    distribute_hour: int = 9
    distribute_minute: int = 0
    distribute_hours: list[int] = []
    distribute_timezone: str = "UTC"
    monthly_newsletter_enabled: bool = True
    monthly_newsletter_day: int = 1
    monthly_newsletter_hour: int = 9
    monthly_newsletter_minute: int = 0
    monthly_newsletter_timezone: str = "UTC"
    shadow_enabled: bool = False
    shadow_minutes_before_publish: int = 45
    shadow_candidate_overrides_path: str = ""
    shadow_include_text: bool = False
    a2a_request_timeout_seconds: int = 180
    generation_run_stale_pending_minutes: int = 180
    analysis_retry_max_attempts: int = 5
    analysis_retry_base_delay_seconds: int = 300
    analysis_retry_max_delay_seconds: int = 7200
    analysis_retry_stale_analyzing_minutes: int = 120

    # Scraping
    scrape_content_limit: int = 10000
    scrape_min_content_length: int = 100
    scrape_playwright_fetch_fallback: bool = True

    # Analysis
    relevance_threshold: int = 7
    briefing_lookback_hours: int = 24
    briefing_max_items: int = 5
    briefing_min_selected_items: int = 1
    briefing_max_ai_items: int = 2
    briefing_max_twitter_items: int = 2
    briefing_max_rss_items: int = 3
    briefing_max_items_per_domain: int = 2
    briefing_history_items: int = 10
    briefing_min_chars: int = 1200
    briefing_target_chars: int = 1700
    briefing_hard_max_chars: int = 2300
    briefing_critique_enabled: bool = True
    briefing_critique_max_rounds: int = 5
    briefing_novelty_lookback_hours: int = 24 * 21
    briefing_novelty_max_items: int = 250
    briefing_novelty_title_similarity_threshold: float = 0.78
    briefing_mix_min_urgent: int = 1
    briefing_mix_min_platform: int = 1
    briefing_mix_min_tooling: int = 1
    briefing_mix_min_regulatory: int = 1
    briefing_min_reddit_engagement_score: float = 24.0
    briefing_min_twitter_engagement_score: float = 180.0
    briefing_social_floor_exempt_relevance: int = 9
    briefing_untrusted_rss_min_score: int = 8
    briefing_quiet_day_enabled: bool = True
    briefing_quiet_day_high_signal_threshold: int = 8
    briefing_quiet_day_min_high_signal_items: int = 3
    briefing_quiet_day_max_items: int = 3
    briefing_quiet_day_min_chars: int = 900
    briefing_quiet_day_target_chars: int = 1300
    briefing_quiet_day_hard_max_chars: int = 1800
    briefing_monthly_min_chars: int = 2600
    briefing_monthly_target_chars: int = 4200
    briefing_monthly_hard_max_chars: int = 7800
    briefing_skip_if_no_high_signal: bool = True

    def agent_port(self, agent_name: str) -> int:
        """Return the configured port for the named local agent service."""
        port_field = self._AGENT_PORT_FIELDS.get(str(agent_name or "").strip().lower())
        if port_field is None:
            raise ValueError(f"Unknown agent name: {agent_name}")
        return int(getattr(self, port_field))

    def agent_url(self, agent_name: str) -> str:
        """Return the resolved A2A base URL for the named agent service."""
        normalized = str(agent_name or "").strip().lower()
        url_field = self._AGENT_URL_FIELDS.get(normalized)
        if url_field is None:
            raise ValueError(f"Unknown agent name: {agent_name}")
        configured_url = str(getattr(self, url_field) or "").strip().rstrip("/")
        if configured_url:
            return configured_url
        return f"http://localhost:{self.agent_port(normalized)}"

    def has_agent_url_overrides(self) -> bool:
        """Return True when any agent endpoint is configured explicitly."""
        return any(
            bool(str(getattr(self, field_name) or "").strip())
            for field_name in self._AGENT_URL_FIELDS.values()
        )
    briefing_min_high_signal_to_publish: int = 1
    briefing_single_item_min_chars: int = 450
    briefing_single_item_target_chars: int = 850
    briefing_single_item_hard_max_chars: int = 1400
    briefing_social_proof_weight: float = 0.35
    briefing_social_proof_max_bonus: float = 2.5
    briefing_gate_mode: str = "balanced"  # strict, balanced, minimal
    briefing_selection_require_reddit: bool = True
    briefing_selection_require_csp: bool = True
    briefing_max_source_share: float = 0.5
    briefing_min_items_after_coverage_drop: int = 1
    briefing_missing_coverage_max_drops: int = 2
    briefing_verifier_enabled: bool = True
    briefing_verifier_max_links: int = 12
    briefing_verifier_url_liveness_timeout_ms: int = 20000
    briefing_verifier_block_on_llm_hard: bool = True
    briefing_critic_min_score: int = 80
    briefing_critic_min_actionability: int = 70
    briefing_critic_min_source_diversity: int = 65
    briefing_critic_min_link_hygiene: int = 80
    monthly_newsletter_lookback_days: int = 31
    monthly_newsletter_min_score: int = 7
    monthly_newsletter_min_items: int = 6
    monthly_newsletter_max_items: int = 12
    monthly_newsletter_max_items_per_domain: int = 3

    # Telegram output
    telegram_overflow_mode: str = "smart"  # smart, always, never
    briefing_distribution_max_draft_age_minutes: int = 180
    distribution_retry_max_attempts: int = 6
    distribution_retry_base_delay_seconds: int = 600
    distribution_retry_max_delay_seconds: int = 21600
    distribution_retry_stale_distributing_minutes: int = 30
