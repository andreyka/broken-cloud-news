"""Component-scoped config surfaces for BCN deployable services."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict

from bcn.common.config import Settings

_COMPONENT_SERVICE_URL_FIELDS = {
    "writer": "writer_service_url",
    "critic": "critic_service_url",
    "verifier": "verifier_service_url",
    "collector": "collector_service_url",
    "analyst": "analyst_service_url",
    "distributor": "distributor_service_url",
}

_COMPONENT_DEFAULT_PORTS = {
    "writer": 8081,
    "critic": 8082,
    "verifier": 8083,
    "collector": 8084,
    "analyst": 8085,
    "distributor": 8086,
}


def _parse_list_value(value: Any) -> Any:
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


class _BCNComponentSettingsBase(BaseSettings):
    """Shared env-loading behavior for component-local settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="BCN_",
        enable_decoding=False,
        extra="ignore",
    )

    service_auth_token: str = ""
    service_request_timeout_seconds: int = 900

    @field_validator(
        "twitter_handles",
        "rss_feeds",
        "ghsa_severities",
        "ghsa_keywords",
        "email_recipients",
        "reddit_subreddits",
        "twitter_required_keywords",
        mode="before",
        check_fields=False,
    )
    @classmethod
    def _empty_str_to_list(cls, value: Any) -> Any:
        return _parse_list_value(value)

    @field_validator(
        "llm_provider",
        mode="before",
        check_fields=False,
    )
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

    @field_validator("telegram_overflow_mode", mode="before", check_fields=False)
    @classmethod
    def _validate_telegram_overflow_mode(cls, value: str) -> str:
        mode = (value or "").strip().lower()
        return mode if mode in {"smart", "always", "never"} else "smart"

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
        if raw.startswith("http://") or raw.startswith("https://"):
            return raw.rstrip("/")
        raise ValueError(f"Invalid service URL '{raw}'")

    @field_validator("service_request_timeout_seconds", mode="before", check_fields=False)
    @classmethod
    def _validate_service_request_timeout(cls, value: int) -> int:
        timeout = int(value)
        if timeout <= 0:
            raise ValueError("service_request_timeout_seconds must be > 0")
        return timeout


class _RoleAwareLLMComponentSettings(_BCNComponentSettingsBase):
    """Shared role-aware LLM settings for LLM-backed services."""

    llm_provider: str = "openai_compat"
    llm_base_url: str = "http://host.docker.internal:8000/v1"
    llm_model: str = "Qwen/Qwen3-VL-30B-A3B-Instruct-FP8"
    llm_api_key: str = ""
    llm_timeout: int = 180
    llm_chat_retries: int = 16
    llm_retry_max_wait_seconds: int = 600
    llm_retry_jitter_min_seconds: float = 0.5
    llm_retry_jitter_max_seconds: float = 5.0
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


class WriterServiceSettings(_RoleAwareLLMComponentSettings):
    """Writer-service runtime settings for standalone deployment."""

    database_url: str = "postgresql://broken_cloud_news:cloud_security@localhost:5432/broken_cloud_news"
    critic_service_url: str = ""
    verifier_service_url: str = ""
    comfyui_url: str = "http://host.docker.internal:8188"
    comfyui_timeout: int = 300
    comfyui_poll_interval: int = 2
    briefing_max_items: int = 5
    briefing_min_selected_items: int = 1
    briefing_max_items_per_domain: int = 2
    briefing_min_chars: int = 1200
    briefing_target_chars: int = 1700
    briefing_hard_max_chars: int = 2300
    briefing_critique_enabled: bool = True
    briefing_critique_max_rounds: int = 5
    briefing_novelty_lookback_hours: int = 24 * 21
    briefing_novelty_max_items: int = 250
    briefing_novelty_title_similarity_threshold: float = 0.78
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
    briefing_min_high_signal_to_publish: int = 1
    briefing_single_item_min_chars: int = 450
    briefing_single_item_target_chars: int = 850
    briefing_single_item_hard_max_chars: int = 1400
    briefing_social_proof_weight: float = 0.35
    briefing_social_proof_max_bonus: float = 2.5
    briefing_gate_mode: str = "balanced"
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
    monthly_newsletter_min_items: int = 6
    monthly_newsletter_max_items: int = 12
    monthly_newsletter_max_items_per_domain: int = 3


class CriticServiceSettings(_RoleAwareLLMComponentSettings):
    """Critic-service runtime settings for standalone deployment."""

    briefing_min_chars: int = 1200
    briefing_target_chars: int = 1700
    briefing_hard_max_chars: int = 2300
    briefing_quiet_day_min_chars: int = 900
    briefing_quiet_day_target_chars: int = 1300
    briefing_quiet_day_hard_max_chars: int = 1800
    briefing_monthly_min_chars: int = 2600
    briefing_monthly_target_chars: int = 4200
    briefing_monthly_hard_max_chars: int = 7800
    briefing_gate_mode: str = "balanced"
    briefing_critic_min_score: int = 80
    briefing_critic_min_actionability: int = 70
    briefing_critic_min_source_diversity: int = 65
    briefing_critic_min_link_hygiene: int = 80


class VerifierServiceSettings(_RoleAwareLLMComponentSettings):
    """Verifier-service runtime settings for standalone deployment."""

    briefing_verifier_max_links: int = 12
    briefing_verifier_url_liveness_timeout_ms: int = 20000
    briefing_verifier_block_on_llm_hard: bool = True


class AnalystServiceSettings(_RoleAwareLLMComponentSettings):
    """Analyst-service runtime settings for standalone deployment."""

    scrape_content_limit: int = 10000
    scrape_min_content_length: int = 100


class CollectorServiceSettings(_BCNComponentSettingsBase):
    """Collector-service runtime settings for standalone deployment."""

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
    scrape_content_limit: int = 10000
    scrape_min_content_length: int = 100
    collector_rss_max_entries_per_feed: int = 40
    collector_rss_max_item_age_days: int = 45
    collector_rss_full_content_limit_per_feed: int = 5
    collector_rss_scrape_timeout_ms: int = 20000


class DistributorServiceSettings(_BCNComponentSettingsBase):
    """Distributor-service runtime settings for standalone deployment."""

    comfyui_url: str = "http://host.docker.internal:8188"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_overflow_mode: str = "smart"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    email_from: str = ""
    email_recipients: list[str] = []
    slack_webhook_url: str = ""
    discord_bot_token: str = ""
    discord_channel_id: str = ""


_COMPONENT_SETTINGS_CLASSES = {
    "writer": WriterServiceSettings,
    "critic": CriticServiceSettings,
    "verifier": VerifierServiceSettings,
    "collector": CollectorServiceSettings,
    "analyst": AnalystServiceSettings,
    "distributor": DistributorServiceSettings,
}


@dataclass(frozen=True)
class ServiceClientSettings:
    """Remote endpoint settings for one BCN component."""

    component: str
    base_url: str
    timeout_seconds: int
    auth_token: str

    @property
    def configured(self) -> bool:
        """Return whether the component is configured for remote calls."""
        return bool(self.base_url)


def load_component_service_settings(component: str) -> BaseSettings:
    """Load only the env/config surface needed by one deployable component."""
    normalized = str(component or "").strip().lower()
    settings_class = _COMPONENT_SETTINGS_CLASSES.get(normalized)
    if settings_class is None:
        raise ValueError(f"Unsupported component: {component}")
    return settings_class()


def service_client_settings(settings: Settings, component: str) -> ServiceClientSettings:
    """Return a narrow remote-client config view for one component."""
    normalized = str(component or "").strip().lower()
    field_name = _COMPONENT_SERVICE_URL_FIELDS.get(normalized)
    if not field_name:
        raise ValueError(f"Unsupported component: {component}")
    return ServiceClientSettings(
        component=normalized,
        base_url=str(getattr(settings, field_name, "") or "").strip(),
        timeout_seconds=max(1, int(settings.service_request_timeout_seconds)),
        auth_token=str(settings.service_auth_token or "").strip(),
    )


def default_service_port(component: str) -> int:
    """Return the default bind port for one deployable component."""
    normalized = str(component or "").strip().lower()
    if normalized not in _COMPONENT_DEFAULT_PORTS:
        raise ValueError(f"Unsupported component: {component}")
    return _COMPONENT_DEFAULT_PORTS[normalized]


__all__ = [
    "AnalystServiceSettings",
    "CollectorServiceSettings",
    "CriticServiceSettings",
    "DistributorServiceSettings",
    "ServiceClientSettings",
    "VerifierServiceSettings",
    "WriterServiceSettings",
    "default_service_port",
    "load_component_service_settings",
    "service_client_settings",
]
