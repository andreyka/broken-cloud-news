from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="BCN_")

    # Database
    database_url: str = "postgresql://n8n:cloud_security_agent@localhost:5432/broken_cloud_news"

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

    # Apify (Twitter/X scraping)
    apify_token: str = ""
    twitter_handles: list[str] = [
        "GoogleVRP", "HackingLZ", "anton_chuvakin", "AISecHub",
        "_JohnHammond", "lauriewired", "chompie1337", "Steph3nSims",
        "d0znpp", "KimZetter", "argvee", "avkovaleff", "tom_doerr",
    ]
    twitter_max_items: int = 20

    # RSS feeds
    rss_feeds: list[str] = [
        "https://www.cisa.gov/cybersecurity-advisories/all.xml",
        "https://aws.amazon.com/blogs/security/feed/",
    ]

    # GHSA filter
    ghsa_severities: list[str] = ["CRITICAL", "HIGH"]
    ghsa_keywords: list[str] = [
        "kubernetes", "k8s", "docker", "container",
        "aws", "azure", "gcp", "cloud", "terraform", "iam",
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
    twitter_interval_hours: int = 6
    analyst_interval_minutes: int = 15
    distribute_hour: int = 9
    distribute_minute: int = 0

    # Scraping
    scrape_content_limit: int = 10000
    scrape_min_content_length: int = 100

    # Analysis
    juiciness_threshold: int = 7
    briefing_lookback_hours: int = 24
