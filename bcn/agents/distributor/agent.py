"""Distributor agent: publishes briefings to Telegram, Email, Slack, and Discord."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import logging
import re
from uuid import UUID

from a2a.server.agent_execution import AgentExecutor
from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from a2a.types import AgentSkill
from a2a.utils import new_agent_text_message
from typing_extensions import override

from bcn.agents.base import enqueue_event_safe
from bcn.common.config import Settings
from bcn.common.db import claim_draft_briefing_by_id
from bcn.common.db import claim_latest_draft_briefing
from bcn.common.db import get_distribution_outcomes
from bcn.common.db import get_newsletter_subscribers
from bcn.common.db import mark_briefing_distributed
from bcn.common.db import mark_items_published
from bcn.common.db import release_briefing_for_retry
from bcn.common.db import upsert_distribution_outcome
from bcn.common.url_policy import trusted_hosts_from_urls
from bcn.distributors import Distributor
from bcn.distributors.discord import DiscordDistributor
from bcn.distributors.email import EmailDistributor
from bcn.distributors.telegram import TelegramDistributor

logger = logging.getLogger(__name__)
REGULAR_DAILY_BRIEFING_MODE = "regular_daily_briefing"
AD_HOC_MODE = "ad_hoc"
REGULAR_MONTHLY_NEWSLETTER_MODE = "regular_monthly_newsletter"
_SUPPORTED_MODES = {
    REGULAR_DAILY_BRIEFING_MODE,
    AD_HOC_MODE,
    REGULAR_MONTHLY_NEWSLETTER_MODE,
}
_UUID_PATTERN = re.compile(
    r"\b[0-9a-fA-F]{8}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}\b"
)

SKILLS = [
    AgentSkill(
        id="distribute_briefing",
        name="Distribute Briefing",
        description="Distribute the latest briefing to Telegram, Email, Slack, and Discord",
        tags=["distribute", "publish"],
        examples=["distribute", "distribute_briefing", "publish"],
    ),
]


class DistributorExecutor(AgentExecutor):
    """A2A agent that sends briefings to configured distribution channels."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _build_channels(
        self,
        mode: str = REGULAR_DAILY_BRIEFING_MODE,
        *,
        newsletter_recipients: list[str] | None = None,
    ) -> list[tuple[str, Distributor]]:
        trusted_image_hosts = trusted_hosts_from_urls([self.settings.comfyui_url])
        channels: list[tuple[str, Distributor]] = []
        normalized_mode = self._normalize_mode(mode)
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

        if normalized_mode == REGULAR_MONTHLY_NEWSLETTER_MODE:
            recipients = list(newsletter_recipients or [])
        else:
            recipients = list(self.settings.email_recipients)

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

    async def close(self) -> None:
        """No persistent resources are held between executions."""

    @override
    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Send the latest DRAFT briefing to all configured channels."""
        user_input = context.get_user_input() or ""
        requested_briefing_id = self._extract_requested_briefing_id(user_input)
        requested_mode = self._extract_requested_mode(user_input)
        if requested_briefing_id:
            briefing = await claim_draft_briefing_by_id(requested_briefing_id)
        else:
            briefing = await claim_latest_draft_briefing()
        if not briefing:
            if requested_briefing_id:
                await enqueue_event_safe(
                    event_queue,
                    new_agent_text_message(
                        f"Requested briefing {requested_briefing_id} is not available for distribution"
                    ),
                )
                return
            await enqueue_event_safe(
                event_queue, new_agent_text_message("No new briefing to distribute")
            )
            return
        should_release_for_retry = True
        channels: list[tuple[str, Distributor]] = []
        results: dict[str, str] = {}
        msg: str | None = None
        mode = self._normalize_mode(requested_mode)
        newsletter_recipients: list[str] | None = None

        try:
            max_age_minutes = max(
                0, int(self.settings.briefing_distribution_max_draft_age_minutes)
            )
            if max_age_minutes > 0:
                created_at = briefing.get("created_at")
                if isinstance(created_at, datetime):
                    created_utc = (
                        created_at.astimezone(timezone.utc)
                        if created_at.tzinfo is not None
                        else created_at.replace(tzinfo=timezone.utc)
                    )
                    age = datetime.now(timezone.utc) - created_utc
                    if age > timedelta(minutes=max_age_minutes):
                        age_minutes = int(age.total_seconds() // 60)
                        await enqueue_event_safe(
                            event_queue,
                            new_agent_text_message(
                                f"Latest draft is stale ({age_minutes} minutes old), skipping distribution"
                            ),
                        )
                        return

            if mode == REGULAR_MONTHLY_NEWSLETTER_MODE:
                subscriber_rows = await get_newsletter_subscribers(active_only=True)
                newsletter_recipients = []
                for row in subscriber_rows:
                    email = str(dict(row).get("email") or "").strip()
                    if email:
                        newsletter_recipients.append(email)
            channels = self._build_channels(
                mode=mode,
                newsletter_recipients=newsletter_recipients,
            )
            if not channels:
                if mode == REGULAR_MONTHLY_NEWSLETTER_MODE and not (
                    newsletter_recipients or []
                ):
                    await enqueue_event_safe(
                        event_queue,
                        new_agent_text_message(
                            "No active monthly newsletter subscribers configured"
                        ),
                    )
                    return
                await enqueue_event_safe(
                    event_queue,
                    new_agent_text_message(
                        f"No distribution channels configured for mode={mode}"
                    ),
                )
                return

            previous = await get_distribution_outcomes(briefing_ids=[briefing["id"]])
            previously_ok_channels: set[str] = set()
            for row in previous:
                try:
                    channel = str(row["channel"]).strip().lower()
                    status = str(row["status"] or "").strip().lower()
                except Exception:
                    continue
                if channel and status == "ok":
                    previously_ok_channels.add(channel)

            for name, channel in channels:
                if name in previously_ok_channels:
                    logger.info(
                        "Skipping channel %s for briefing %s (already sent successfully)",
                        name,
                        briefing["id"],
                    )
                    results[name] = "ok"
                    continue

                status = "failed"
                metadata: dict[str, object] = {}
                external_message_id: str | None = None
                try:
                    channel_briefing = dict(briefing)
                    if mode == REGULAR_MONTHLY_NEWSLETTER_MODE:
                        created_at = briefing.get("created_at")
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
                        metadata = dict(channel.last_result)
                        msg_id = metadata.get("primary_message_id")
                        if msg_id is not None:
                            external_message_id = str(msg_id)

                    status = "ok" if ok else "failed"
                    results[name] = status
                except Exception:
                    logger.exception("Distribution to %s failed", name)
                    status = "error"
                    results[name] = status
                    metadata = {"error": "exception_during_send"}

                try:
                    await upsert_distribution_outcome(
                        briefing_id=briefing["id"],
                        channel=name,
                        status=status,
                        external_message_id=external_message_id,
                        metrics={},
                        metadata=metadata,
                    )
                except Exception:
                    logger.exception(
                        "Failed to persist distribution outcome for %s", name
                    )

            all_ok = bool(results) and all(
                status == "ok" for status in results.values()
            )
            if all_ok:
                await mark_briefing_distributed(briefing["id"], results)
                item_ids = list(briefing["item_ids"]) if briefing["item_ids"] else []
                await mark_items_published(item_ids)
                should_release_for_retry = False
                msg = f"Distributed to: {results} (mode={mode})"
            else:
                msg = (
                    "Distribution incomplete; kept briefing as DRAFT for retry. "
                    f"mode={mode} results={results}"
                )
        finally:
            for _name, channel in channels:
                try:
                    await channel.close()
                except Exception:
                    logger.warning("Failed to close %s channel", _name)
            if should_release_for_retry:
                try:
                    await release_briefing_for_retry(briefing["id"])
                except Exception:
                    logger.exception(
                        "Failed to release briefing %s back to DRAFT",
                        briefing["id"],
                    )

        if msg:
            logger.info(msg)
            await enqueue_event_safe(event_queue, new_agent_text_message(msg))

    @staticmethod
    def _extract_requested_briefing_id(text: str) -> UUID | None:
        """Extract optional target briefing UUID from distributor skill text."""
        match = _UUID_PATTERN.search(text or "")
        if not match:
            return None
        try:
            return UUID(match.group(0))
        except ValueError:
            return None

    @staticmethod
    def _extract_requested_mode(text: str) -> str | None:
        """Extract optional distribution mode from skill text tokens."""
        for token in str(text or "").split("::"):
            candidate = token.strip().lower()
            if candidate.startswith("mode="):
                candidate = candidate.split("=", 1)[1].strip().lower()
            if candidate in _SUPPORTED_MODES:
                return candidate
        return None

    @staticmethod
    def _normalize_mode(mode: str | None) -> str:
        normalized = str(mode or "").strip().lower()
        return (
            normalized
            if normalized in _SUPPORTED_MODES
            else REGULAR_DAILY_BRIEFING_MODE
        )

    @override
    async def cancel(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Cancel is not supported."""
        raise NotImplementedError("cancel not supported")
