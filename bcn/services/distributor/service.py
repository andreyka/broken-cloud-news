"""Distributor domain service shared by the control plane and transport adapters."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from bcn.common.secrets import redact_sensitive_value
from bcn.common.url_policy import trusted_hosts_from_urls
from bcn.contracts.distributor import ChannelDeliveryResult
from bcn.contracts.distributor import DeliveryRequest
from bcn.contracts.distributor import DeliveryResult
from bcn.contracts.distributor import REGULAR_DAILY_BRIEFING_MODE
from bcn.contracts.distributor import REGULAR_MONTHLY_NEWSLETTER_MODE
from bcn.contracts.modes import WEEKLY_FLAGSHIP_MODE
from bcn.contracts.distributor import normalize_distribution_mode
from bcn.contracts.distributor import parse_delivery_request_payload
from bcn.contracts.distributor import parse_delivery_result_payload
from bcn.contracts.distributor import render_delivery_request_payload
from bcn.contracts.distributor import render_delivery_result_payload
from bcn.distributors import Distributor
from bcn.distributors.discord import DiscordDistributor
from bcn.distributors.email import EmailDistributor
from bcn.distributors.ghost import GhostDistributor
from bcn.distributors.substack import SubstackDistributor
from bcn.distributors.telegram import TelegramDistributor

logger = logging.getLogger(__name__)


def _distribution_redaction_secrets(settings: object) -> tuple[str, ...]:
    """Return configured secrets that must never reach logs or DB metadata."""
    return tuple(
        value.strip()
        for value in (
            getattr(settings, "telegram_bot_token", ""),
            getattr(settings, "discord_bot_token", ""),
            getattr(settings, "slack_webhook_url", ""),
            getattr(settings, "smtp_password", ""),
            getattr(settings, "substack_sid", ""),
            getattr(settings, "ghost_admin_api_key", ""),
        )
        if str(value or "").strip()
    )


def _trusted_image_hosts(settings: object) -> list[str]:
    """Return the distributor allowlist for externally hosted cover images."""
    configured_urls = list(getattr(settings, "trusted_image_source_urls", []) or [])
    comfyui_url = str(getattr(settings, "comfyui_url", "") or "").strip()
    if comfyui_url and comfyui_url not in configured_urls:
        configured_urls.append(comfyui_url)
    return trusted_hosts_from_urls(configured_urls)


class DistributorService:
    """Domain service for pure briefing delivery without workflow DB mutations."""

    def __init__(self, settings: object) -> None:
        self.settings = settings
        self._redaction_secrets = _distribution_redaction_secrets(settings)

    async def close(self) -> None:
        """No persistent resources are held between executions."""

    def _build_channels(
        self,
        *,
        mode: str,
        newsletter_recipients: list[str] | None = None,
    ) -> list[tuple[str, Distributor]]:
        """Build channel clients for one delivery mode."""
        trusted_image_hosts = _trusted_image_hosts(self.settings)
        channels: list[tuple[str, Distributor]] = []
        normalized_mode = normalize_distribution_mode(mode)
        if normalized_mode == REGULAR_MONTHLY_NEWSLETTER_MODE:
            channel_names = {"email"}
        elif normalized_mode == WEEKLY_FLAGSHIP_MODE:
            # The flagship is inbox/web-native; the realtime wire channels
            # (telegram/discord) are deliberately excluded.
            channel_names = {"substack", "ghost", "email"}
        else:
            channel_names = {"telegram", "discord", "ghost", "substack"}

        if (
            "telegram" in channel_names
            and self.settings.telegram_bot_token
            and self.settings.telegram_chat_id
        ):
            channels.append(
                (
                    "telegram",
                    TelegramDistributor(
                        self.settings.telegram_bot_token,
                        self.settings.telegram_chat_id,
                        overflow_mode=self.settings.telegram_overflow_mode,
                        trusted_image_hosts=trusted_image_hosts,
                    ),
                )
            )

        recipients = (
            list(newsletter_recipients or [])
            if normalized_mode
            in (REGULAR_MONTHLY_NEWSLETTER_MODE, WEEKLY_FLAGSHIP_MODE)
            else list(self.settings.email_recipients)
        )
        if "email" in channel_names and self.settings.smtp_host and recipients:
            channels.append(
                (
                    "email",
                    EmailDistributor(
                        smtp_host=self.settings.smtp_host,
                        smtp_port=self.settings.smtp_port,
                        smtp_user=self.settings.smtp_user,
                        smtp_password=self.settings.smtp_password,
                        from_addr=self.settings.email_from,
                        recipients=recipients,
                    ),
                )
            )

        if (
            "discord" in channel_names
            and self.settings.discord_bot_token
            and self.settings.discord_channel_id
        ):
            channels.append(
                (
                    "discord",
                    DiscordDistributor(
                        self.settings.discord_bot_token,
                        self.settings.discord_channel_id,
                        trusted_image_hosts=trusted_image_hosts,
                    ),
                )
            )

        if (
            "ghost" in channel_names
            and getattr(self.settings, "ghost_enabled", False)
            and self.settings.ghost_admin_api_url
            and self.settings.ghost_admin_api_key
        ):
            channels.append(
                (
                    "ghost",
                    GhostDistributor(
                        admin_api_url=self.settings.ghost_admin_api_url,
                        admin_api_key=self.settings.ghost_admin_api_key,
                        trusted_image_hosts=trusted_image_hosts,
                    ),
                )
            )
        if (
            "substack" in channel_names
            and getattr(self.settings, "substack_enabled", False)
            and self.settings.substack_sid
            and self.settings.substack_publication_url
        ):
            channels.append(
                (
                    "substack",
                    SubstackDistributor(
                        publication_url=self.settings.substack_publication_url,
                        sid=self.settings.substack_sid,
                        trusted_image_hosts=trusted_image_hosts,
                        ghost_admin_api_url=self.settings.ghost_admin_api_url,
                        ghost_admin_api_key=self.settings.ghost_admin_api_key,
                    ),
                )
            )
        return channels

    async def deliver(self, request: DeliveryRequest) -> DeliveryResult:
        """Deliver an explicit briefing payload to configured channels."""
        mode = normalize_distribution_mode(request.mode)
        channels: list[tuple[str, Distributor]] = []
        results: dict[str, str] = {}
        attempts: list[ChannelDeliveryResult] = []

        try:
            channels = self._build_channels(
                mode=mode,
                newsletter_recipients=list(request.newsletter_recipients),
            )
            if not channels:
                if mode == REGULAR_MONTHLY_NEWSLETTER_MODE and not request.newsletter_recipients:
                    return DeliveryResult(
                        mode=mode,
                        results={},
                        attempts=(),
                        all_ok=False,
                        message="No active monthly newsletter subscribers configured",
                    )
                return DeliveryResult(
                    mode=mode,
                    results={},
                    attempts=(),
                    all_ok=False,
                    message=f"No distribution channels configured for mode={mode}",
                )

            for name, channel in channels:
                if name in request.previous_ok_channels:
                    logger.info(
                        "Skipping channel %s for briefing %s (already sent successfully)",
                        name,
                        request.briefing.get("id"),
                    )
                    results[name] = "ok"
                    continue

                status = "failed"
                metadata: dict[str, Any] = {}
                external_message_id: str | None = None
                try:
                    channel_briefing = dict(request.briefing)
                    briefing_title = str(
                        request.briefing.get("title") or ""
                    ).strip()
                    if mode == REGULAR_MONTHLY_NEWSLETTER_MODE:
                        created_at = request.briefing.get("created_at")
                        month_label = (
                            created_at.strftime("%B %Y")
                            if isinstance(created_at, datetime)
                            else "Monthly Edition"
                        )
                        channel_briefing["email_subject"] = (
                            f"Broken Cloud News Monthly Newsletter - {month_label}"
                        )
                    elif briefing_title:
                        # Substack/Ghost/email use this as the post title
                        # instead of the created-at timestamp fallback.
                        channel_briefing["email_subject"] = briefing_title

                    ok = await channel.send(channel_briefing)
                    if isinstance(channel.last_result, dict):
                        metadata = redact_sensitive_value(
                            dict(channel.last_result),
                            secrets=self._redaction_secrets,
                        )
                        message_id = metadata.get("primary_message_id")
                        if message_id is not None:
                            external_message_id = str(message_id)

                    status = "ok" if ok else "failed"
                    results[name] = status
                except Exception:
                    logger.exception("Distribution to %s failed", name)
                    status = "error"
                    results[name] = status
                    metadata = {"error": "exception_during_send"}

                attempts.append(
                    ChannelDeliveryResult(
                        channel=name,
                        status=status,
                        external_message_id=external_message_id,
                        metadata=metadata,
                    )
                )

            all_ok = bool(results) and all(
                status == "ok" for status in results.values()
            )
            if all_ok:
                message = f"Distributed to: {results} (mode={mode})"
            else:
                message = (
                    "Distribution incomplete; kept briefing as DRAFT for retry. "
                    f"mode={mode} results={results}"
                )
            return DeliveryResult(
                mode=mode,
                results=results,
                attempts=tuple(attempts),
                all_ok=all_ok,
                message=message,
            )
        finally:
            for name, channel in channels:
                try:
                    await channel.close()
                except Exception:
                    logger.warning("Failed to close %s channel", name)


__all__ = [
    "ChannelDeliveryResult",
    "DeliveryRequest",
    "DeliveryResult",
    "DistributorService",
    "normalize_distribution_mode",
    "parse_delivery_request_payload",
    "parse_delivery_result_payload",
    "render_delivery_request_payload",
    "render_delivery_result_payload",
]
