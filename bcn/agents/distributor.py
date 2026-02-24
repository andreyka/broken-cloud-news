"""Distributor agent: publishes briefings to Telegram, Email, and Slack."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from typing_extensions import override

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import AgentSkill
from a2a.utils import new_agent_text_message

from bcn.agents.base import enqueue_event_safe
from bcn.config import Settings
from bcn.db import (
    get_latest_briefing,
    mark_briefing_distributed,
    mark_items_published,
    upsert_distribution_outcome,
)
from bcn.distributors.email import EmailDistributor
from bcn.distributors.slack import SlackDistributor
from bcn.distributors.telegram import TelegramDistributor

logger = logging.getLogger(__name__)

SKILLS = [
    AgentSkill(
        id="distribute_briefing",
        name="Distribute Briefing",
        description="Distribute the latest briefing to Telegram, Email, and Slack",
        tags=["distribute", "publish"],
        examples=["distribute", "distribute_briefing", "publish"],
    ),
]


class DistributorExecutor(AgentExecutor):
    """A2A agent that sends briefings to configured distribution channels."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.channels: list[tuple[str, TelegramDistributor | EmailDistributor | SlackDistributor]] = []

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
                new_agent_text_message("No new briefing to distribute")
            )
            return

        if not self.channels:
            await enqueue_event_safe(
                event_queue,
                new_agent_text_message("No distribution channels configured")
            )
            return

        results: dict[str, str] = {}
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        for name, channel in self.channels:
            status = "failed"
            metadata: dict[str, object] = {}
            external_message_id: str | None = None
            try:
                if isinstance(channel, TelegramDistributor):
                    ok = await channel.send(
                        markdown=briefing["content_markdown"],
                        cover_image_url=briefing["cover_image_url"],
                    )
                    metadata = dict(channel.last_result) if isinstance(channel.last_result, dict) else {}
                    msg_id = metadata.get("primary_message_id")
                    if msg_id is not None:
                        external_message_id = str(msg_id)
                elif isinstance(channel, EmailDistributor):
                    ok = await channel.send(
                        subject=f"Broken Cloud News - {today}",
                        html_body=(
                            briefing["content_html"]
                            or briefing["content_markdown"]
                        ),
                    )
                    metadata = {"recipient_count": len(channel.recipients)}
                elif isinstance(channel, SlackDistributor):
                    ok = await channel.send(
                        markdown=briefing["content_markdown"],
                        cover_image_url=briefing["cover_image_url"],
                    )
                    metadata = {
                        "cover_image": bool(briefing["cover_image_url"]),
                        "markdown_chars": len(str(briefing["content_markdown"] or "")),
                    }
                else:
                    ok = False

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
                logger.exception("Failed to persist distribution outcome for %s", name)

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
