"""Reusable settings sections shared by control-plane and service settings."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError

from pydantic import field_validator
from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict


def _parse_list_env_value(value: Any) -> Any:
    """Allow empty strings, JSON arrays, or plain lists for env-backed lists."""
    if isinstance(value, str) and value.strip() == "":
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value
        if isinstance(parsed, list):
            return parsed
    return value


def _normalize_provider(value: str, *, allow_empty: bool) -> str:
    raw = (value or "").strip().lower()
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
    if not raw:
        return "" if allow_empty else "openai_compat"
    return aliases.get(raw, "openai_compat")


class BCNSettingsBase(BaseSettings):
    """Shared env loading and validation behavior for BCN settings models."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="BCN_",
        enable_decoding=False,
        extra="ignore",
    )

    @field_validator(
        "twitter_handles",
        "rss_feeds",
        "ghsa_severities",
        "ghsa_keywords",
        "email_recipients",
        "reddit_subreddits",
        "trusted_image_source_urls",
        "twitter_required_keywords",
        "distribute_hours",
        mode="before",
        check_fields=False,
    )
    @classmethod
    def _empty_str_to_list(cls, value: Any) -> Any:
        return _parse_list_env_value(value)

    @field_validator("distribute_hours", mode="before", check_fields=False)
    @classmethod
    def _parse_distribute_hours(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            raw = value.strip()
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
        if isinstance(value, (int, float)):
            return [int(value)]
        return value

    @field_validator("distribute_hours", check_fields=False)
    @classmethod
    def _validate_distribute_hours(cls, value: list[Any]) -> list[int]:
        normalized: list[int] = []
        seen: set[int] = set()
        for raw in value or []:
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

    @field_validator(
        "distribute_timezone",
        "monthly_newsletter_timezone",
        "ai_review_backfill_timezone",
        mode="before",
        check_fields=False,
    )
    @classmethod
    def _validate_schedule_timezone(cls, value: str) -> str:
        normalized = (value or "").strip() or "UTC"
        try:
            ZoneInfo(normalized)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Invalid timezone '{normalized}'") from exc
        return normalized

    @field_validator("monthly_newsletter_day", check_fields=False)
    @classmethod
    def _validate_monthly_day(cls, value: int) -> int:
        day = int(value)
        if day < 1 or day > 28:
            raise ValueError("monthly_newsletter_day must be between 1 and 28")
        return day

    @field_validator("monthly_newsletter_hour", check_fields=False)
    @classmethod
    def _validate_monthly_hour(cls, value: int) -> int:
        hour = int(value)
        if hour < 0 or hour > 23:
            raise ValueError("monthly_newsletter_hour must be between 0 and 23")
        return hour

    @field_validator("monthly_newsletter_minute", check_fields=False)
    @classmethod
    def _validate_monthly_minute(cls, value: int) -> int:
        minute = int(value)
        if minute < 0 or minute > 59:
            raise ValueError("monthly_newsletter_minute must be between 0 and 59")
        return minute

    @field_validator("ai_review_backfill_hour", check_fields=False)
    @classmethod
    def _validate_ai_review_backfill_hour(cls, value: int) -> int:
        hour = int(value)
        if hour < 0 or hour > 23:
            raise ValueError("ai_review_backfill_hour must be between 0 and 23")
        return hour

    @field_validator("ai_review_backfill_minute", check_fields=False)
    @classmethod
    def _validate_ai_review_backfill_minute(cls, value: int) -> int:
        minute = int(value)
        if minute < 0 or minute > 59:
            raise ValueError("ai_review_backfill_minute must be between 0 and 59")
        return minute

    @field_validator("ai_review_backfill_weekday", mode="before", check_fields=False)
    @classmethod
    def _validate_ai_review_backfill_weekday(cls, value: str) -> str:
        raw = str(value or "").strip().lower() or "sun"
        allowed = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
        if raw not in allowed:
            raise ValueError(
                "ai_review_backfill_weekday must be one of: mon, tue, wed, thu, fri, sat, sun"
            )
        return raw

    @field_validator("ai_review_backfill_max_briefings", check_fields=False)
    @classmethod
    def _validate_ai_review_backfill_max_briefings(cls, value: int) -> int:
        limit = int(value)
        if limit <= 0:
            raise ValueError("ai_review_backfill_max_briefings must be > 0")
        return limit

    @field_validator("shadow_minutes_before_publish", check_fields=False)
    @classmethod
    def _validate_shadow_minutes_before_publish(cls, value: int) -> int:
        minutes = int(value)
        if minutes < 0 or minutes >= 24 * 60:
            raise ValueError("shadow_minutes_before_publish must be between 0 and 1439")
        return minutes

    @field_validator("telegram_overflow_mode", mode="before", check_fields=False)
    @classmethod
    def _validate_telegram_overflow_mode(cls, value: str) -> str:
        mode = (value or "").strip().lower()
        return mode if mode in {"smart", "always", "never"} else "smart"

    @field_validator("briefing_gate_mode", mode="before", check_fields=False)
    @classmethod
    def _validate_briefing_gate_mode(cls, value: str) -> str:
        mode = (value or "").strip().lower()
        return mode if mode in {"strict", "balanced", "minimal"} else "balanced"

    @field_validator("llm_provider", mode="before", check_fields=False)
    @classmethod
    def _validate_llm_provider(cls, value: str) -> str:
        return _normalize_provider(value, allow_empty=False)

    @field_validator(
        "llm_provider_analyst",
        "llm_provider_writer",
        "llm_provider_critic",
        "llm_provider_verifier",
        "llm_provider_cover",
        mode="before",
        check_fields=False,
    )
    @classmethod
    def _validate_llm_role_provider(cls, value: str) -> str:
        return _normalize_provider(value, allow_empty=True)

    @field_validator(
        "writer_service_url",
        "critic_service_url",
        "verifier_service_url",
        "collector_service_url",
        "analyst_service_url",
        "distributor_service_url",
        mode="before",
        check_fields=False,
    )
    @classmethod
    def _validate_service_url(cls, value: str) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        parsed = urlsplit(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"Invalid service URL '{raw}'")
        return raw.rstrip("/")

    @field_validator("service_request_timeout_seconds", mode="before", check_fields=False)
    @classmethod
    def _validate_service_request_timeout(cls, value: int) -> int:
        timeout = int(value)
        if timeout <= 0:
            raise ValueError("service_request_timeout_seconds must be > 0")
        return timeout


class DatabaseSettingsMixin:
    """Database connectivity settings."""

    database_url: str = "postgresql://postgres:postgres@localhost:5432/broken_cloud_news"


class ServiceTransportSettingsMixin:
    """Shared transport settings for inter-service HTTP calls."""

    service_request_timeout_seconds: int = 900
    service_auth_token: str = ""


class RemoteComponentEndpointSettingsMixin:
    """Remote endpoint settings for all deployable components."""

    writer_service_url: str = ""
    critic_service_url: str = ""
    verifier_service_url: str = ""
    collector_service_url: str = ""
    analyst_service_url: str = ""
    distributor_service_url: str = ""


class ReviewServiceEndpointSettingsMixin:
    """Remote endpoint settings for review services consumed by writer."""

    critic_service_url: str = ""
    verifier_service_url: str = ""


class SharedLLMSettingsMixin:
    """Shared LLM and role-override settings."""

    llm_provider: str = "openai_compat"
    llm_base_url: str = "http://localhost:8000/v1"
    llm_model: str = "Qwen/Qwen3-VL-30B-A3B-Instruct"
    llm_api_key: str = ""
    llm_timeout: int = 180
    llm_chat_retries: int = 16
    llm_retry_max_wait_seconds: int = 600
    llm_retry_jitter_min_seconds: float = 0.5
    llm_retry_jitter_max_seconds: float = 5.0
    llm_timeout_analyst: int | None = None
    llm_timeout_writer: int | None = None
    llm_timeout_critic: int | None = None
    llm_timeout_verifier: int | None = None
    llm_timeout_cover: int | None = None
    llm_chat_retries_analyst: int | None = None
    llm_retry_max_wait_seconds_analyst: int | None = None
    llm_retry_jitter_min_seconds_analyst: float | None = None
    llm_retry_jitter_max_seconds_analyst: float | None = None
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
    llm_reasoning_effort: str = ""
    llm_reasoning_effort_analyst: str = ""
    llm_reasoning_effort_writer: str = ""
    llm_reasoning_effort_critic: str = ""
    llm_reasoning_effort_verifier: str = ""
    llm_reasoning_effort_cover: str = ""
    writer_prompt_bundle_path: str = ""
    critic_prompt_path: str = ""
    verifier_prompt_path: str = ""

    @field_validator(
        "llm_timeout",
        "llm_timeout_analyst",
        "llm_timeout_writer",
        "llm_timeout_critic",
        "llm_timeout_verifier",
        "llm_timeout_cover",
        mode="before",
        check_fields=False,
    )
    @classmethod
    def _validate_llm_timeout(cls, value: int | None) -> int | None:
        if value is None:
            return None
        timeout = int(value)
        if timeout <= 0:
            raise ValueError("llm_timeout must be > 0")
        return timeout

    @field_validator(
        "llm_chat_retries",
        "llm_chat_retries_analyst",
        mode="before",
        check_fields=False,
    )
    @classmethod
    def _validate_llm_chat_retries(cls, value: int | None) -> int | None:
        if value is None:
            return None
        retries = int(value)
        if retries <= 0:
            raise ValueError("llm_chat_retries must be > 0")
        return retries

    @field_validator(
        "llm_retry_max_wait_seconds",
        "llm_retry_max_wait_seconds_analyst",
        mode="before",
        check_fields=False,
    )
    @classmethod
    def _validate_llm_retry_max_wait_seconds(cls, value: int | None) -> int | None:
        if value is None:
            return None
        seconds = int(value)
        if seconds <= 0:
            raise ValueError("llm_retry_max_wait_seconds must be > 0")
        return seconds

    @field_validator(
        "llm_retry_jitter_min_seconds",
        "llm_retry_jitter_max_seconds",
        "llm_retry_jitter_min_seconds_analyst",
        "llm_retry_jitter_max_seconds_analyst",
        mode="before",
        check_fields=False,
    )
    @classmethod
    def _validate_llm_retry_jitter_seconds(cls, value: float | None) -> float | None:
        if value is None:
            return None
        seconds = float(value)
        if seconds < 0:
            raise ValueError("llm retry jitter seconds must be >= 0")
        return seconds


class ComfyUISettingsMixin:
    """Image-generation endpoint settings."""

    comfyui_url: str = "http://localhost:8188"
    comfyui_timeout: int = 300
    comfyui_poll_interval: int = 2


class AIReviewSettingsMixin:
    """OpenAI-backed editorial review settings."""

    ai_review_api_key: str = ""
    ai_review_base_url: str = "https://api.openai.com/v1"
    ai_review_model: str = "gpt-5.4"
    ai_review_reasoning_effort: str = "high"
    ai_review_timeout_seconds: int = 180
    ai_review_auto_enabled: bool = True
    ai_review_publish_gate_enabled: bool = False

    @field_validator("ai_review_base_url", mode="before", check_fields=False)
    @classmethod
    def _validate_ai_review_base_url(cls, value: str) -> str:
        raw = str(value or "").strip()
        if not raw:
            return "https://api.openai.com/v1"
        parsed = urlsplit(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"Invalid AI review base URL '{raw}'")
        return raw.rstrip("/")

    @field_validator("ai_review_reasoning_effort", mode="before", check_fields=False)
    @classmethod
    def _validate_ai_review_reasoning_effort(cls, value: str) -> str:
        raw = str(value or "").strip().lower()
        if raw in {"", "low", "medium", "high", "xhigh"}:
            return raw
        raise ValueError(
            "ai_review_reasoning_effort must be one of: '', low, medium, high, xhigh"
        )

    @field_validator("ai_review_timeout_seconds", mode="before", check_fields=False)
    @classmethod
    def _validate_ai_review_timeout_seconds(cls, value: int) -> int:
        timeout = int(value)
        if timeout <= 0:
            raise ValueError("ai_review_timeout_seconds must be > 0")
        return timeout


class TrustedImageSourceSettingsMixin:
    """Trusted upstream image hosts used by distribution clients."""

    trusted_image_source_urls: list[str] = []


class CollectionSourceSettingsMixin:
    """External source credentials and source lists."""

    github_token: str = ""
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
    source_review_enabled: bool = True
    source_review_sample_size: int = 4
    rss_feeds: list[str] = [
        "https://www.cisa.gov/cybersecurity-advisories/all.xml",
        "https://aws.amazon.com/blogs/security/feed/",
        "https://blog.cloudflare.com/tag/security/rss/",
        "https://unit42.paloaltonetworks.com/feed/",
        "https://research.checkpoint.com/feed/",
        "https://www.wiz.io/feed/rss.xml",
    ]
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


class SchedulingSettingsMixin:
    """Schedule and retry settings owned by the control plane."""

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
    weekly_flagship_enabled: bool = False
    weekly_flagship_day_of_week: str = "thu"
    weekly_flagship_hour: int = 16
    weekly_flagship_minute: int = 0
    weekly_flagship_timezone: str = "UTC"
    weekly_flagship_lookback_days: int = 7
    weekly_flagship_min_score: int = 7
    weekly_flagship_min_items: int = 5
    weekly_flagship_max_items: int = 10
    briefing_weekly_min_chars: int = 3000
    briefing_weekly_target_chars: int = 5000
    briefing_weekly_hard_max_chars: int = 9000
    shadow_enabled: bool = False
    shadow_minutes_before_publish: int = 45
    shadow_candidate_overrides_path: str = ""
    shadow_include_text: bool = False
    ai_review_backfill_enabled: bool = True
    ai_review_backfill_weekday: str = "sun"
    ai_review_backfill_hour: int = 4
    ai_review_backfill_minute: int = 0
    ai_review_backfill_timezone: str = "UTC"
    ai_review_backfill_max_briefings: int = 25
    generation_run_stale_pending_minutes: int = 180
    analysis_retry_max_attempts: int = 5
    analysis_retry_base_delay_seconds: int = 300
    analysis_retry_max_delay_seconds: int = 7200
    analysis_retry_stale_analyzing_minutes: int = 120


class WorkflowQueueSettingsMixin:
    """Durable workflow queue and worker settings."""

    workflow_job_poll_interval_seconds: int = 5
    workflow_job_lease_refresh_seconds: int = 60
    workflow_job_default_lease_seconds: int = 900
    workflow_job_publish_lease_seconds: int = 1200
    workflow_job_collection_lease_seconds: int = 900
    workflow_job_analysis_lease_seconds: int = 1200
    workflow_job_evaluation_lease_seconds: int = 1800
    workflow_job_publish_deadline_seconds: int = 7200
    workflow_job_collection_deadline_seconds: int = 5400
    workflow_job_analysis_deadline_seconds: int = 3600
    workflow_job_evaluation_deadline_seconds: int = 43200
    workflow_job_retry_base_delay_seconds: int = 30
    workflow_job_retry_max_delay_seconds: int = 300
    workflow_job_publish_retry_base_delay_seconds: int = 30
    workflow_job_publish_retry_max_delay_seconds: int = 300
    workflow_job_collection_retry_base_delay_seconds: int = 120
    workflow_job_collection_retry_max_delay_seconds: int = 1800
    workflow_job_analysis_retry_base_delay_seconds: int = 120
    workflow_job_analysis_retry_max_delay_seconds: int = 1800
    workflow_job_evaluation_retry_base_delay_seconds: int = 300
    workflow_job_evaluation_retry_max_delay_seconds: int = 3600


class ScrapingSettingsMixin:
    """HTML scraping and RSS body-fetch limits."""

    scrape_content_limit: int = 10000
    scrape_min_content_length: int = 100
    scrape_playwright_fetch_fallback: bool = True
    collector_rss_max_entries_per_feed: int = 40
    collector_rss_max_item_age_days: int = 45
    collector_rss_full_content_limit_per_feed: int = 5
    collector_rss_scrape_timeout_ms: int = 20000


class CriticPolicySettingsMixin:
    """Critic gate thresholds and scoring policy."""

    briefing_gate_mode: str = "balanced"
    briefing_critic_min_score: int = 80
    briefing_critic_min_actionability: int = 70
    briefing_critic_min_source_diversity: int = 65
    briefing_critic_min_link_hygiene: int = 80


class VerifierPolicySettingsMixin:
    """Verifier policy settings."""

    briefing_verifier_enabled: bool = True
    briefing_verifier_max_links: int = 12
    briefing_verifier_url_liveness_timeout_ms: int = 20000
    briefing_verifier_block_on_llm_hard: bool = True
    briefing_verifier_overreach_rewrites: int = 1


class BriefingLengthSettingsMixin:
    """Shared briefing and newsletter length limits."""

    briefing_min_chars: int = 1200
    briefing_target_chars: int = 1700
    briefing_hard_max_chars: int = 2300
    briefing_scaling_base_items: int = 3
    briefing_extra_item_target_chars: int = 350
    briefing_extra_item_hard_max_chars: int = 400
    briefing_quiet_day_min_chars: int = 900
    briefing_quiet_day_target_chars: int = 1300
    briefing_quiet_day_hard_max_chars: int = 1800
    briefing_monthly_min_chars: int = 2600
    briefing_monthly_target_chars: int = 4200
    briefing_monthly_hard_max_chars: int = 7800
    briefing_single_item_min_chars: int = 450
    briefing_single_item_target_chars: int = 850
    briefing_single_item_hard_max_chars: int = 1400


class WriterPolicySettingsMixin(
    BriefingLengthSettingsMixin,
    CriticPolicySettingsMixin,
    VerifierPolicySettingsMixin,
):
    """Writer selection, quality, and newsletter generation policy."""

    relevance_threshold: int = 7
    briefing_lookback_hours: int = 24
    briefing_max_items: int = 5
    briefing_min_selected_items: int = 1
    briefing_max_ai_items: int = 2
    briefing_max_twitter_items: int = 2
    briefing_max_rss_items: int = 3
    briefing_max_items_per_domain: int = 2
    briefing_history_items: int = 10
    briefing_critique_enabled: bool = True
    briefing_critique_max_rounds: int = 7
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
    briefing_skip_if_no_high_signal: bool = True
    briefing_min_high_signal_to_publish: int = 1
    briefing_social_proof_weight: float = 0.35
    briefing_social_proof_max_bonus: float = 2.5
    briefing_selection_require_reddit: bool = True
    briefing_selection_require_csp: bool = True
    briefing_max_source_share: float = 0.5
    briefing_min_items_after_coverage_drop: int = 1
    briefing_missing_coverage_max_drops: int = 2
    monthly_newsletter_lookback_days: int = 31
    monthly_newsletter_min_score: int = 7
    monthly_newsletter_min_items: int = 6
    monthly_newsletter_max_items: int = 12
    monthly_newsletter_max_items_per_domain: int = 3


class DeliveryChannelSettingsMixin:
    """Outbound channel credentials and recipient settings."""

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    alerts_enabled: bool = False
    alert_telegram_chat_id: str = ""
    alert_quiet_streak_threshold: int = 4
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    email_from: str = ""
    email_recipients: list[str] = []
    slack_webhook_url: str = ""
    discord_bot_token: str = ""
    discord_channel_id: str = ""
    substack_enabled: bool = False
    substack_sid: str = ""
    substack_publication_url: str = ""
    ghost_enabled: bool = False
    ghost_admin_api_url: str = ""
    ghost_admin_api_key: str = ""


class DistributionPolicySettingsMixin:
    """Delivery policy and retry settings."""

    telegram_overflow_mode: str = "smart"
    briefing_distribution_max_draft_age_minutes: int = 180
    distribution_retry_max_attempts: int = 6
    distribution_retry_base_delay_seconds: int = 600
    distribution_retry_max_delay_seconds: int = 21600
    distribution_retry_stale_distributing_minutes: int = 30


__all__ = [
    "BCNSettingsBase",
    "BriefingLengthSettingsMixin",
    "CollectionSourceSettingsMixin",
    "ComfyUISettingsMixin",
    "CriticPolicySettingsMixin",
    "DatabaseSettingsMixin",
    "DeliveryChannelSettingsMixin",
    "DistributionPolicySettingsMixin",
    "RemoteComponentEndpointSettingsMixin",
    "ReviewServiceEndpointSettingsMixin",
    "SchedulingSettingsMixin",
    "ScrapingSettingsMixin",
    "ServiceTransportSettingsMixin",
    "SharedLLMSettingsMixin",
    "TrustedImageSourceSettingsMixin",
    "VerifierPolicySettingsMixin",
    "WorkflowQueueSettingsMixin",
    "WriterPolicySettingsMixin",
]
