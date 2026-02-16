"""Distributor agent: publishes briefings to Telegram, Email, and Slack."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from typing_extensions import override

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import AgentSkill
from a2a.utils import new_agent_text_message

from bcn.config import Settings
from bcn.db import get_latest_briefing, mark_briefing_distributed, mark_items_published
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
                    settings.telegram_bot_token, settings.telegram_chat_id
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
            event_queue.enqueue_event(
                new_agent_text_message("No new briefing to distribute")
            )
            return

        if not self.channels:
            event_queue.enqueue_event(
                new_agent_text_message("No distribution channels configured")
            )
            return

        results: dict[str, str] = {}
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        for name, channel in self.channels:
            try:
                if isinstance(channel, TelegramDistributor):
                    ok = await channel.send(
                        markdown=briefing["content_markdown"],
                        cover_image_url=briefing["cover_image_url"],
                    )
                elif isinstance(channel, EmailDistributor):
                    ok = await channel.send(
                        subject=f"Broken Cloud News - {today}",
                        html_body=(
                            briefing["content_html"]
                            or briefing["content_markdown"]
                        ),
                    )
                elif isinstance(channel, SlackDistributor):
                    ok = await channel.send(
                        markdown=briefing["content_markdown"],
                        cover_image_url=briefing["cover_image_url"],
                    )
                else:
                    ok = False

                results[name] = "ok" if ok else "failed"
            except Exception:
                logger.exception("Distribution to %s failed", name)
                results[name] = "error"

        await mark_briefing_distributed(briefing["id"], results)
        item_ids = list(briefing["item_ids"]) if briefing["item_ids"] else []
        await mark_items_published(item_ids)

        msg = f"Distributed to: {results}"
        logger.info(msg)
        event_queue.enqueue_event(new_agent_text_message(msg))

    @override
    async def cancel(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Cancel is not supported."""
        raise NotImplementedError("cancel not supported")
