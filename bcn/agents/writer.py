"""Writer agent: generates daily briefings with cover images."""

from __future__ import annotations

import logging
import re
import time

from typing_extensions import override

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import AgentSkill
from a2a.utils import new_agent_text_message

from bcn.comfyui import ComfyUIClient
from bcn.config import Settings
from bcn.db import get_analyzed_items, insert_briefing
from bcn.llm import LLMClient

logger = logging.getLogger(__name__)

SKILLS = [
    AgentSkill(
        id="generate_briefing",
        name="Generate Briefing",
        description="Generate a security briefing with cover image from top-scored items",
        tags=["briefing", "writer"],
        examples=["write", "generate_briefing", "generate briefing"],
    ),
]


class WriterExecutor(AgentExecutor):
    """A2A agent that composes briefings from top-scored items."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.llm = LLMClient(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            timeout=settings.llm_timeout,
        )
        self.comfyui = ComfyUIClient(
            base_url=settings.comfyui_url,
            timeout=settings.comfyui_timeout,
            poll_interval=settings.comfyui_poll_interval,
        )

    @override
    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Generate a briefing from top-scored items and store it as DRAFT."""
        items = await get_analyzed_items(
            min_score=self.settings.relevance_threshold,
            hours=self.settings.briefing_lookback_hours,
        )

        if not items:
            msg = (
                f"Quiet day — no items scored >= {self.settings.relevance_threshold} "
                f"in the last {self.settings.briefing_lookback_hours}h. "
                f"Skipping briefing."
            )
            logger.info(msg)
            event_queue.enqueue_event(new_agent_text_message(msg))
            return

        items = items[:5]

        briefing_body = await self.llm.generate_briefing(items)
        logger.info("LLM briefing generated (%d chars)", len(briefing_body))

        topics = "\n".join(f"- {i['title']}: {i['summary']}" for i in items)
        cover_prompt = await self.llm.generate_cover_prompt(topics)
        logger.info("Cover prompt: %s", cover_prompt[:100])

        cover_url = ""
        try:
            timestamp = int(time.time() * 1000)
            prefix = f"Digest_Cover_{timestamp}"
            cover_url = await self.comfyui.generate_image(cover_prompt, prefix)
            logger.info("Cover image: %s", cover_url)
        except Exception:
            logger.exception("Failed to generate cover image, continuing without it")

        markdown = self._format_markdown(briefing_body, cover_url)
        html = self._format_html(briefing_body, cover_url)

        item_ids = [i["id"] for i in items]
        briefing_id = await insert_briefing(
            content_markdown=markdown,
            content_html=html,
            cover_image_url=cover_url,
            cover_image_prompt=cover_prompt,
            item_ids=item_ids,
        )

        msg = f"Briefing {briefing_id} created with {len(items)} items"
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

    @staticmethod
    def _format_markdown(briefing_body: str, cover_url: str) -> str:
        """Wrap the briefing body with an optional cover image in Markdown.

        Args:
            briefing_body: The LLM-generated briefing text.
            cover_url: URL of the cover image (may be empty).

        Returns:
            Complete Markdown document.
        """
        md = ""
        if cover_url:
            md += f"![Daily Cover]({cover_url})\n\n"
        md += briefing_body
        return md

    @staticmethod
    def _format_html(briefing_body: str, cover_url: str) -> str:
        """Convert the briefing body to basic HTML.

        Handles ``###``/``##`` headers, bold, italic, links, and paragraphs.

        Args:
            briefing_body: The LLM-generated briefing text.
            cover_url: URL of the cover image (may be empty).

        Returns:
            A minimal HTML document string.
        """
        html_body = briefing_body
        html_body = re.sub(
            r"^### (.+)$", r"<h3>\1</h3>", html_body, flags=re.MULTILINE
        )
        html_body = re.sub(
            r"^## (.+)$", r"<h2>\1</h2>", html_body, flags=re.MULTILINE
        )
        html_body = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html_body)
        html_body = re.sub(r"\*(.+?)\*", r"<em>\1</em>", html_body)
        html_body = re.sub(
            r"\[([^\]]+)]\(([^)]+)\)", r'<a href="\2">\1</a>', html_body
        )
        html_body = re.sub(r"\n{2,}", "</p>\n<p>", html_body)
        html_body = f"<p>{html_body}</p>"

        parts = ["<html><body>"]
        if cover_url:
            parts.append(
                f'<img src="{cover_url}" alt="Daily Cover" '
                f'style="max-width:600px"/>'
            )
        parts.append(html_body)
        parts.append("</body></html>")
        return "\n".join(parts)
