"""Distributor domain service shared by the control plane and transport adapters."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
import json
import logging
from typing import Any

from bcn.common.config import Settings
from bcn.common.secrets import redact_sensitive_value
from bcn.common.url_policy import trusted_hosts_from_urls
from bcn.distributors import Distributor
from bcn.distributors.discord import DiscordDistributor
from bcn.distributors.email import EmailDistributor
from bcn.distributors.telegram import TelegramDistributor

logger = logging.getLogger(__name__)

REGULAR_DAILY_BRIEFING_MODE = "regular_daily_briefing"
REGULAR_MONTHLY_NEWSLETTER_MODE = "regular_monthly_newsletter"
_SUPPORTED_MODES = frozenset(
    (
        REGULAR_DAILY_BRIEFING_MODE,
        "ad_hoc",
        REGULAR_MONTHLY_NEWSLETTER_MODE,
    )
)
_DELIVERY_REQUEST_PREFIX = "deliver_briefing::"
_DELIVERY_RESULT_PREFIX = "delivery_result::"


@dataclass(frozen=True)
class ChannelDeliveryResult:
    """Structured outcome for one channel send attempt."""

    channel: str
    status: str
    external_message_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryRequest:
    """Explicit distributor worker input prepared by the control plane."""

    briefing: dict[str, Any]
    mode: str
    newsletter_recipients: tuple[str, ...] = ()
    previous_ok_channels: frozenset[str] = frozenset()


@dataclass(frozen=True)
class DeliveryResult:
    """Structured distributor worker output consumed by the control plane."""

    mode: str
    results: dict[str, str]
    attempts: tuple[ChannelDeliveryResult, ...]
    all_ok: bool
    message: str


def normalize_distribution_mode(mode: str | None) -> str:
    """Return a supported distribution mode or the daily default."""
    normalized = str(mode or "").strip().lower()
    if normalized in _SUPPORTED_MODES:
        return normalized
    return REGULAR_DAILY_BRIEFING_MODE


def render_delivery_request_payload(request: DeliveryRequest) -> str:
    """Render a structured delivery request for agent transport."""
    payload = {
        "briefing": dict(request.briefing),
        "mode": normalize_distribution_mode(request.mode),
        "newsletter_recipients": list(request.newsletter_recipients),
        "previous_ok_channels": sorted(request.previous_ok_channels),
    }
    return _DELIVERY_REQUEST_PREFIX + json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )


def parse_delivery_request_payload(text: str) -> DeliveryRequest | None:
    """Parse a structured delivery request from agent input text."""
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line.startswith(_DELIVERY_REQUEST_PREFIX):
            continue

        raw_payload = line[len(_DELIVERY_REQUEST_PREFIX) :].strip()
        if not raw_payload:
            continue

        try:
            decoded = json.loads(raw_payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(decoded, dict):
            continue

        raw_briefing = decoded.get("briefing")
        if not isinstance(raw_briefing, dict):
            continue

        mode = normalize_distribution_mode(decoded.get("mode"))
        briefing = dict(raw_briefing)

        raw_created_at = briefing.get("created_at")
        if isinstance(raw_created_at, str) and raw_created_at.strip():
            try:
                briefing["created_at"] = datetime.fromisoformat(
                    raw_created_at.replace("Z", "+00:00")
                )
            except ValueError:
                pass

        recipients = tuple(
            str(value).strip()
            for value in decoded.get("newsletter_recipients", [])
            if str(value).strip()
        )
        previous_ok_channels = frozenset(
            str(value).strip().lower()
            for value in decoded.get("previous_ok_channels", [])
            if str(value).strip()
        )
        return DeliveryRequest(
            briefing=briefing,
            mode=mode,
            newsletter_recipients=recipients,
            previous_ok_channels=previous_ok_channels,
        )
    return None


def render_delivery_result_payload(result: DeliveryResult) -> str:
    """Render a structured delivery result for agent transport."""
    payload = {
        "all_ok": bool(result.all_ok),
        "attempts": [
            {
                "channel": attempt.channel,
                "external_message_id": attempt.external_message_id,
                "metadata": attempt.metadata,
                "status": attempt.status,
            }
            for attempt in result.attempts
        ],
        "message": result.message,
        "mode": normalize_distribution_mode(result.mode),
        "results": dict(result.results),
    }
    return _DELIVERY_RESULT_PREFIX + json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )


def parse_delivery_result_payload(text: str) -> DeliveryResult | None:
    """Parse a structured delivery result from agent output text."""
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line.startswith(_DELIVERY_RESULT_PREFIX):
            continue

        raw_payload = line[len(_DELIVERY_RESULT_PREFIX) :].strip()
        if not raw_payload:
            continue

        try:
            decoded = json.loads(raw_payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(decoded, dict):
            continue

        results_raw = decoded.get("results")
        if not isinstance(results_raw, dict):
            continue
        results = {
            str(channel).strip().lower(): str(status).strip().lower()
            for channel, status in results_raw.items()
            if str(channel).strip()
        }

        attempts_raw = decoded.get("attempts", [])
        if not isinstance(attempts_raw, list):
            continue
        attempts: list[ChannelDeliveryResult] = []
        for item in attempts_raw:
            if not isinstance(item, dict):
                continue
            channel = str(item.get("channel") or "").strip().lower()
            status = str(item.get("status") or "").strip().lower()
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            if not channel or not status:
                continue
            attempts.append(
                ChannelDeliveryResult(
                    channel=channel,
                    status=status,
                    external_message_id=(
                        str(item.get("external_message_id")).strip()
                        if item.get("external_message_id") is not None
                        else None
                    ),
                    metadata=dict(metadata),
                )
            )

        return DeliveryResult(
            mode=normalize_distribution_mode(decoded.get("mode")),
            results=results,
            attempts=tuple(attempts),
            all_ok=bool(decoded.get("all_ok")),
            message=str(decoded.get("message") or "").strip(),
        )
    return None


def _distribution_redaction_secrets(settings: Settings) -> tuple[str, ...]:
    """Return configured secrets that must never reach logs or DB metadata."""
    return tuple(
        value.strip()
        for value in (
            settings.telegram_bot_token,
            settings.discord_bot_token,
            settings.slack_webhook_url,
            settings.smtp_password,
        )
        if str(value or "").strip()
    )


class DistributorService:
    """Domain service for pure briefing delivery without workflow DB mutations."""

    def __init__(self, settings: Settings) -> None:
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
        trusted_image_hosts = trusted_hosts_from_urls([self.settings.comfyui_url])
        channels: list[tuple[str, Distributor]] = []
        normalized_mode = normalize_distribution_mode(mode)
        if normalized_mode == REGULAR_MONTHLY_NEWSLETTER_MODE:
            channel_names = {"email"}
        else:
            channel_names = {"telegram", "discord"}

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
            if normalized_mode == REGULAR_MONTHLY_NEWSLETTER_MODE
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
