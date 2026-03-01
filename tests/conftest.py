from __future__ import annotations

import pytest


@pytest.fixture
def settings():
    """Return a Settings instance with harmless defaults (no real services)."""
    from bcn.common.config import Settings

    return Settings(
        database_url="postgresql://test:test@localhost:5432/test",
        llm_base_url="http://localhost:9999/v1",
        llm_model="test-model",
        comfyui_url="http://localhost:9998",
        github_token="ghp_fake",
        apify_token="apify_fake",
        browserless_url="http://localhost:9997",
        telegram_bot_token="123:FAKE",
        telegram_chat_id="-100123",
        smtp_host="smtp.example.com",
        smtp_user="user",
        smtp_password="pass",
        email_from="test@example.com",
        email_recipients=["a@b.com"],
        slack_webhook_url="https://hooks.slack.com/fake",
        generation_run_stale_pending_minutes=0,
    )
