from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from bcn.services.distributor.service import _distribution_redaction_secrets
from bcn.common.config import Settings
from bcn.common.secrets import redact_sensitive_text
from bcn.common.secrets import redact_sensitive_value
from bcn.distributors.slack import SlackDistributor
from bcn.distributors.telegram import TelegramDistributor


def test_redact_sensitive_text_masks_known_token_shapes():
    telegram_token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcd"
    webhook_url = "https://hooks.slack.com/services/T00/B00/very-secret-webhook"
    raw = (
        f"request failed for /bot{telegram_token}/sendMessage "
        f"Authorization: Bot discord-super-secret "
        f"{webhook_url}"
    )

    redacted = redact_sensitive_text(
        raw,
        secrets=("discord-super-secret",),
    )

    assert telegram_token not in redacted
    assert webhook_url not in redacted
    assert "discord-super-secret" not in redacted
    assert "[REDACTED]" in redacted


def test_redact_sensitive_value_masks_nested_distribution_metadata():
    telegram_token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcd"
    webhook_url = "https://hooks.slack.com/services/T00/B00/very-secret-webhook"
    settings = Settings(
        telegram_bot_token=telegram_token,
        discord_bot_token="discord-super-secret",
        slack_webhook_url=webhook_url,
        smtp_password="smtp-password",
        ghost_admin_api_key="ghost-id:" + ("ab" * 32),
    )

    payload = {
        "error": f"POST /bot{telegram_token}/sendMessage failed",
        "nested": [
            webhook_url,
            "Authorization: Bot discord-super-secret",
            {"smtp": "smtp-password"},
            {"ghost": "ghost-id:" + ("ab" * 32)},
        ],
    }

    sanitized = redact_sensitive_value(
        payload,
        secrets=_distribution_redaction_secrets(settings),
    )
    text = json.dumps(sanitized)

    assert telegram_token not in text
    assert webhook_url not in text
    assert "discord-super-secret" not in text
    assert "smtp-password" not in text
    assert "ghost-id:" not in text


@pytest.mark.asyncio
async def test_telegram_distributor_does_not_log_raw_bot_token(caplog):
    token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcd"
    distributor = TelegramDistributor(token, "-1001234567890")
    distributor._client.post = AsyncMock(
        side_effect=RuntimeError(
            f"request failed for https://api.telegram.org/bot{token}/sendMessage"
        )
    )

    with caplog.at_level("ERROR", logger="bcn.distributors.telegram"):
        ok = await distributor.send({"content_markdown": "hello"})

    assert ok is False
    assert token not in caplog.text
    assert token not in json.dumps(distributor.last_result)
    assert "[REDACTED]" in caplog.text
    await distributor.close()


@pytest.mark.asyncio
async def test_slack_distributor_does_not_log_raw_webhook(caplog):
    webhook_url = "https://hooks.slack.com/services/T00/B00/very-secret-webhook"
    distributor = SlackDistributor(webhook_url)
    distributor._client.post = AsyncMock(
        side_effect=RuntimeError(f"request failed for {webhook_url}")
    )

    with caplog.at_level("ERROR", logger="bcn.distributors.slack"):
        ok = await distributor.send({"content_markdown": "hello"})

    assert ok is False
    assert webhook_url not in caplog.text
    assert webhook_url not in json.dumps(distributor.last_result)
    assert "[REDACTED]" in caplog.text
    await distributor.close()
