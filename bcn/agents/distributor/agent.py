"""Distributor agent: publishes briefings to Telegram, Email, Slack, and Discord."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import logging
from typing import Any

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
from bcn.common.db import upsert_distribution_outcome
from bcn.distributors.discord import DiscordDistributor
from bcn.distributors.email import EmailDistributor
from bcn.distributors.slack import SlackDistributor
from bcn.distributors.telegram import TelegramDistributor

logger = logging.getLogger(__name__)

SKILLS = [
    AgentSkill(
        id="distribute_briefing",
        name="Distribute Briefing",
        description=
        "Distribute the latest briefing to Telegram, Email, Slack, and Discord",
        tags=["distribute", "publish"],
        examples=["distribute", "distribute_briefing", "publish"],
    ),
]


class DistributorExecutor(AgentExecutor):
    """A2A agent that sends briefings to configured distribution channels."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.channels: list[tuple[str, Any]] = []

        if settings.telegram_bot_token and settings.telegram_chat_id:
            self.channels.append((
                "telegram",
                TelegramDistributor(
                    settings.telegram_bot_token,
                    settings.telegram_chat_id,
                    overflow_mode=settings.telegram_overflow_mode,
                ),
            ))

        if settings.smtp_host and settings.email_recipients:
            self.channels.append((
                "email",
                EmailDistributor(
                    smtp_host=settings.smtp_host,
                    smtp_port=settings.smtp_port,
                    smtp_user=settings.smtp_user,
                    smtp_password=settings.smtp_password,
                    from_addr=settings.email_from,
                    recipients=settings.email_recipients,
                ),
            ))

        if settings.slack_webhook_url:
            self.channels.append((
                "slack",
                SlackDistributor(settings.slack_webhook_url),
            ))

        if settings.discord_bot_token and settings.discord_channel_id:
            self.channels.append((
                "discord",
                DiscordDistributor(
                    settings.discord_bot_token,
                    settings.discord_channel_id,
                ),
            ))

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
                event_queue,
                new_agent_text_message("No new briefing to distribute"))
            return
        max_age_minutes = max(
            0, int(self.settings.briefing_distribution_max_draft_age_minutes))
        if max_age_minutes > 0:
            created_at = briefing.get("created_at")
            if isinstance(created_at, datetime):
                created_utc = (created_at.astimezone(timezone.utc)
                               if created_at.tzinfo is not None else
                               created_at.replace(tzinfo=timezone.utc))
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

        if not self.channels:
            await enqueue_event_safe(
                event_queue,
                new_agent_text_message("No distribution channels configured"))
            return

        results: dict[str, str] = {}

        for name, channel in self.channels:
            status = "failed"
            metadata: dict[str, object] = {}
            external_message_id: str | None = None
            try:
                ok = await channel.send(briefing)

                if hasattr(channel, "last_result") and isinstance(
                        channel.last_result, dict):
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
                    "Failed to persist distribution outcome for %s", name)

        await mark_briefing_distributed(briefing["id"], results)
        item_ids = list(briefing["item_ids"]) if briefing["item_ids"] else []
        await mark_items_published(item_ids)

        msg = f"Distributed to: {results}"
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
