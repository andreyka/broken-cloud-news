"""Distributor agent: publishes briefings to Telegram, Email, Slack, and Discord."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import logging

from a2a.server.agent_execution import AgentExecutor
from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from a2a.types import AgentSkill
from a2a.utils import new_agent_text_message
from typing_extensions import override

from bcn.agents.base import enqueue_event_safe
from bcn.common.config import Settings
from bcn.common.db import get_latest_briefing
from bcn.common.db import mark_briefing_distributed
from bcn.common.db import mark_items_published
from bcn.common.db import get_distribution_outcomes
from bcn.common.db import upsert_distribution_outcome
from bcn.common.url_policy import trusted_hosts_from_urls
from bcn.distributors import Distributor
from bcn.distributors.discord import DiscordDistributor
from bcn.distributors.email import EmailDistributor
from bcn.distributors.slack import SlackDistributor
from bcn.distributors.telegram import TelegramDistributor

logger = logging.getLogger(__name__)

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

    def _build_channels(self) -> list[tuple[str, Distributor]]:
        trusted_image_hosts = trusted_hosts_from_urls([self.settings.comfyui_url])
        channels: list[tuple[str, Distributor]] = []

        if self.settings.telegram_bot_token and self.settings.telegram_chat_id:
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

        if self.settings.smtp_host and self.settings.email_recipients:
            channels.append(
                (
                    "email",
                    EmailDistributor(
                        smtp_host=self.settings.smtp_host,
                        smtp_port=self.settings.smtp_port,
                        smtp_user=self.settings.smtp_user,
                        smtp_password=self.settings.smtp_password,
                        from_addr=self.settings.email_from,
                        recipients=self.settings.email_recipients,
                    ),
                )
            )

        if self.settings.slack_webhook_url:
            channels.append(
                (
                    "slack",
                    SlackDistributor(self.settings.slack_webhook_url),
                )
            )

        if self.settings.discord_bot_token and self.settings.discord_channel_id:
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
        briefing = await get_latest_briefing()
        if not briefing:
            await enqueue_event_safe(
                event_queue, new_agent_text_message("No new briefing to distribute")
            )
            return
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

        channels = self._build_channels()
        if not channels:
            await enqueue_event_safe(
                event_queue,
                new_agent_text_message("No distribution channels configured"),
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

        results: dict[str, str] = {}

        try:
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
                    ok = await channel.send(briefing)

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
        finally:
            for _name, channel in channels:
                try:
                    await channel.close()
                except Exception:
                    logger.warning("Failed to close %s channel", _name)

        all_ok = bool(results) and all(status == "ok" for status in results.values())
        if all_ok:
            await mark_briefing_distributed(briefing["id"], results)
            item_ids = list(briefing["item_ids"]) if briefing["item_ids"] else []
            await mark_items_published(item_ids)
            msg = f"Distributed to: {results}"
        else:
            msg = (
                "Distribution incomplete; kept briefing as DRAFT for retry. "
                f"results={results}"
            )

        logger.info(msg)
        await enqueue_event_safe(event_queue, new_agent_text_message(msg))

    @override
    async def cancel(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Cancel is not supported."""
        raise NotImplementedError("cancel not supported")
