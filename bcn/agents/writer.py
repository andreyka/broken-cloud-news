from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

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
    def __init__(self, settings: Settings):
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
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        items = await get_analyzed_items(
            min_score=self.settings.relevance_threshold,
            hours=self.settings.briefing_lookback_hours,
        )

        if not items:
            event_queue.enqueue_event(
                new_agent_text_message("No items meet threshold for briefing")
            )
            return

        # Take top 5
        items = items[:5]

        # Generate creative briefing via LLM
        briefing_body = await self.llm.generate_briefing(items)
        logger.info("LLM briefing generated (%d chars)", len(briefing_body))

        # Generate cover image prompt from topics
        topics = "\n".join(f"- {i['title']}: {i['summary']}" for i in items)
        cover_prompt = await self.llm.generate_cover_prompt(topics)
        logger.info("Cover prompt: %s", cover_prompt[:100])

        # Generate cover image via ComfyUI Flux
        cover_url = ""
        try:
            timestamp = int(time.time() * 1000)
            prefix = f"Digest_Cover_{timestamp}"
            cover_url = await self.comfyui.generate_image(cover_prompt, prefix)
            logger.info("Cover image: %s", cover_url)
        except Exception:
            logger.exception("Failed to generate cover image, continuing without it")

        # Assemble final markdown and HTML
        markdown = self._format_markdown(briefing_body, cover_url)
        html = self._format_html(briefing_body, cover_url)

        # Store briefing
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
    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise Exception("cancel not supported")

    @staticmethod
    def _format_markdown(briefing_body: str, cover_url: str) -> str:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        md = f"*Broken Cloud Daily Briefing — {today}*\n\n"
        if cover_url:
            md += f"![Daily Cover]({cover_url})\n\n"
        md += briefing_body
        return md

    @staticmethod
    def _format_html(briefing_body: str, cover_url: str) -> str:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # Convert markdown to basic HTML: headers, bold, links, paragraphs
        import re

        html_body = briefing_body
        # ### headers
        html_body = re.sub(r"^### (.+)$", r"<h3>\1</h3>", html_body, flags=re.MULTILINE)
        ## headers
        html_body = re.sub(r"^## (.+)$", r"<h2>\1</h2>", html_body, flags=re.MULTILINE)
        # bold
        html_body = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html_body)
        # italic
        html_body = re.sub(r"\*(.+?)\*", r"<em>\1</em>", html_body)
        # links
        html_body = re.sub(r"\[([^\]]+)]\(([^)]+)\)", r'<a href="\2">\1</a>', html_body)
        # paragraphs
        html_body = re.sub(r"\n{2,}", "</p>\n<p>", html_body)
        html_body = f"<p>{html_body}</p>"

        parts = [
            "<html><body>",
            f"<h1>Broken Cloud Daily Briefing — {today}</h1>",
        ]
        if cover_url:
            parts.append(f'<img src="{cover_url}" alt="Daily Cover" style="max-width:600px"/>')
        parts.append(html_body)
        parts.append("</body></html>")
        return "\n".join(parts)
