"""Application settings loaded from environment variables."""

from __future__ import annotations

from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """BCN configuration backed by ``BCN_``-prefixed environment variables."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="BCN_")

    @field_validator(
        "twitter_handles", "rss_feeds", "ghsa_severities",
        "ghsa_keywords", "email_recipients", "reddit_subreddits",
        "twitter_required_keywords",
        mode="before",
    )
    @classmethod
    def _empty_str_to_list(cls, v: Any) -> Any:
        """Allow empty env-var strings to fall back to the field default."""
        if isinstance(v, str) and v.strip() == "":
            return []
        return v

    @field_validator("telegram_overflow_mode")
    @classmethod
    def _validate_telegram_overflow_mode(cls, v: str) -> str:
        mode = (v or "").strip().lower()
        return mode if mode in {"smart", "always", "never"} else "smart"

    # Database
    database_url: str = "postgresql://broken_cloud_news_agent_db:cloud_security_agent@localhost:5432/broken_cloud_news"

    # LLM (Qwen on DGX Spark)
    llm_base_url: str = "http://192.168.0.9:8000/v1"
    llm_model: str = "Qwen/Qwen3-VL-30B-A3B-Instruct-FP8"
    llm_timeout: int = 120

    # ComfyUI (Flux on DGX Spark)
    comfyui_url: str = "http://192.168.0.9:8188"
    comfyui_timeout: int = 300
    comfyui_poll_interval: int = 2

    # GitHub
    github_token: str = ""

    # X API (Twitter)
    twitter_bearer_token: str = ""
    twitter_handles: list[str] = [
        "GoogleVRP", "HackingLZ", "anton_chuvakin", "AISecHub",
        "_JohnHammond", "lauriewired", "chompie1337", "Steph3nSims",
        "d0znpp", "KimZetter", "argvee", "avkovaleff", "tom_doerr",
    ]
    twitter_max_items: int = 20
    twitter_required_keywords: list[str] = [
        "cloud", "aws", "azure", "gcp", "kubernetes", "k8s",
        "container", "docker", "terraform", "iam", "cve", "vuln",
        "exploit", "rce", "advisory", "serverless", "cloudflare",
        "envoy", "qemu", "kvm", "postgres", "clickhouse", "redis",
        "load balancer",
    ]

    # RSS feeds
    rss_feeds: list[str] = [
        "https://www.cisa.gov/cybersecurity-advisories/all.xml",
        "https://aws.amazon.com/blogs/security/feed/",
        "https://blog.cloudflare.com/tag/security/rss/",
        "https://unit42.paloaltonetworks.com/feed/",
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
        "kubernetes", "k8s", "docker", "container",
        "aws", "azure", "gcp", "cloud", "terraform", "iam",
        "envoy", "qemu", "kvm", "postgres", "clickhouse",
        "redis", "cloudflare", "load balancer",
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

    # Agent ports
    collector_port: int = 9001
    analyst_port: int = 9002
    writer_port: int = 9003
    distributor_port: int = 9004

    # Scheduling
    ghsa_interval_hours: int = 4
    rss_interval_hours: int = 2
    reddit_interval_hours: int = 3
    twitter_interval_hours: int = 6
    analyst_interval_minutes: int = 15
    distribute_hour: int = 9
    distribute_minute: int = 0
    a2a_request_timeout_seconds: int = 180

    # Scraping
    scrape_content_limit: int = 10000
    scrape_min_content_length: int = 100

    # Analysis
    relevance_threshold: int = 7
    briefing_lookback_hours: int = 24
    briefing_max_items: int = 5
    briefing_max_ai_items: int = 2
    briefing_max_twitter_items: int = 2
    briefing_max_rss_items: int = 3
    briefing_max_items_per_domain: int = 2
    briefing_history_items: int = 6
    briefing_min_chars: int = 1200
    briefing_target_chars: int = 1700
    briefing_hard_max_chars: int = 2300

    # Telegram output
    telegram_overflow_mode: str = "smart"  # smart, always, never
