"""Writer agent: generates daily briefings with cover images."""

from __future__ import annotations

import logging
import re
import time
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlparse

from typing_extensions import override

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import AgentSkill
from a2a.utils import new_agent_text_message

from bcn.comfyui import ComfyUIClient
from bcn.config import Settings
from bcn.db import get_analyzed_items, get_recent_briefings, insert_briefing
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

        selected_items = self._select_items_for_briefing([dict(i) for i in items])
        if not selected_items:
            msg = "No items remained after quality/diversity filtering. Skipping briefing."
            logger.info(msg)
            event_queue.enqueue_event(new_agent_text_message(msg))
            return

        history = await get_recent_briefings(limit=self.settings.briefing_history_items)
        history_items = [dict(r) for r in history]

        briefing_body = await self.llm.generate_briefing(
            selected_items,
            recent_briefings=history_items,
        )
        briefing_body = self._dedupe_markdown_links(briefing_body)

        missing_items = self._missing_items_for_markdown(briefing_body, selected_items)
        if missing_items or len(briefing_body) < self.settings.briefing_min_chars:
            briefing_body = await self.llm.enrich_briefing(
                briefing_body,
                selected_items,
                min_chars=self.settings.briefing_min_chars,
                target_chars=self.settings.briefing_target_chars,
                hard_max_chars=self.settings.briefing_hard_max_chars,
                missing_urls=[str(i.get("url", "")) for i in missing_items],
            )
            briefing_body = self._dedupe_markdown_links(briefing_body)

        if len(briefing_body) > self.settings.briefing_hard_max_chars:
            briefing_body = await self.llm.tighten_briefing(
                briefing_body,
                target_chars=self.settings.briefing_target_chars,
                hard_max_chars=self.settings.briefing_hard_max_chars,
            )
            briefing_body = self._dedupe_markdown_links(briefing_body)
            briefing_body = self._clip_markdown(
                briefing_body, self.settings.briefing_hard_max_chars
            )

        missing_items = self._missing_items_for_markdown(briefing_body, selected_items)
        if missing_items:
            briefing_body = self._append_missing_items_section(briefing_body, missing_items)
            briefing_body = self._dedupe_markdown_links(briefing_body)
            briefing_body = self._clip_markdown(
                briefing_body, self.settings.briefing_hard_max_chars
            )

        logger.info("LLM briefing generated (%d chars)", len(briefing_body))

        topics = "\n".join(f"- {i['title']}: {i['summary']}" for i in selected_items)
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

        item_ids = [i["id"] for i in selected_items]
        briefing_id = await insert_briefing(
            content_markdown=markdown,
            content_html=html,
            cover_image_url=cover_url,
            cover_image_prompt=cover_prompt,
            item_ids=item_ids,
        )

        msg = f"Briefing {briefing_id} created with {len(selected_items)} items"
        logger.info(msg)
        event_queue.enqueue_event(new_agent_text_message(msg))

    def _select_items_for_briefing(self, items: list[dict]) -> list[dict]:
        """Select a diverse set of actionable items for the daily briefing."""
        ranked = sorted(
            items,
            key=lambda i: (
                int(i.get("relevance_score", 0)),
                self._parse_timestamp(i.get("published_at")),
            ),
            reverse=True,
        )

        max_items = self.settings.briefing_max_items
        max_ai = self.settings.briefing_max_ai_items
        max_twitter = self.settings.briefing_max_twitter_items
        max_rss = self.settings.briefing_max_rss_items
        max_per_domain = self.settings.briefing_max_items_per_domain

        selected: list[dict] = []
        selected_ids: set[str] = set()
        source_counts: Counter[str] = Counter()
        domain_counts: Counter[str] = Counter()
        ai_count = 0

        def can_add(item: dict, *, relax_soft_limits: bool = False) -> bool:
            nonlocal ai_count
            item_id = str(item.get("id"))
            if item_id in selected_ids:
                return False
            source = str(item.get("source_type", "")).lower()
            if source == "twitter" and source_counts[source] >= max_twitter and not relax_soft_limits:
                return False
            if source == "rss" and source_counts[source] >= max_rss:
                return False
            domain = self._extract_domain(str(item.get("url", "")))
            if domain and max_per_domain > 0 and domain_counts[domain] >= max_per_domain:
                return False
            if self._is_ai_heavy(item) and ai_count >= max_ai and not relax_soft_limits:
                return False
            return True

        def add(item: dict) -> None:
            nonlocal ai_count
            item_id = str(item.get("id"))
            selected.append(item)
            selected_ids.add(item_id)
            source = str(item.get("source_type", "")).lower()
            source_counts[source] += 1
            domain = self._extract_domain(str(item.get("url", "")))
            if domain:
                domain_counts[domain] += 1
            if self._is_ai_heavy(item):
                ai_count += 1

        # Pass 1: ensure representation from major sources when available.
        for source in ("ghsa", "reddit", "rss", "twitter"):
            for item in ranked:
                if len(selected) >= max_items:
                    break
                if str(item.get("source_type", "")).lower() != source:
                    continue
                if not can_add(item):
                    continue
                add(item)
                break

        # Pass 2: prioritize actionable high-score items.
        for item in ranked:
            if len(selected) >= max_items:
                break
            if not self._is_actionable(item):
                continue
            if can_add(item):
                add(item)

        # Pass 3: fill remaining slots by score with guardrails.
        for item in ranked:
            if len(selected) >= max_items:
                break
            if can_add(item):
                add(item)

        # Pass 4: if constraints were too strict, relax only AI/Twitter limits.
        if len(selected) < max_items:
            for item in ranked:
                if len(selected) >= max_items:
                    break
                if can_add(item, relax_soft_limits=True):
                    add(item)

        # Pass 5: if we still have too few items, fully relax caps to avoid empty digests.
        if len(selected) < min(max_items, 3):
            for item in ranked:
                if len(selected) >= max_items:
                    break
                item_id = str(item.get("id"))
                if item_id in selected_ids:
                    continue
                add(item)

        return selected

    @staticmethod
    def _parse_timestamp(value: object) -> datetime:
        """Parse flexible timestamp values from DB records for sorting."""
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                pass
        return datetime.min.replace(tzinfo=timezone.utc)

    @staticmethod
    def _is_actionable(item: dict) -> bool:
        text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
        return bool(re.search(
            r"(cve-\d{4}-\d+|ghsa-|rce|exploit|bypass|patch|fix|advisory|"
            r"incident|breach|credential|auth|ssrf|privesc|container escape)",
            text,
        ))

    @staticmethod
    def _is_ai_heavy(item: dict) -> bool:
        text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
        has_ai = bool(re.search(r"\b(ai|llm|agentic|model|prompt)\b", text))
        has_cloud_security = bool(re.search(
            r"(cloud|kubern|k8s|container|iam|terraform|aws|azure|gcp|"
            r"serverless|envoy|postgres|clickhouse|redis|qemu|kvm|cve|ghsa)",
            text,
        ))
        return has_ai and not has_cloud_security

    @staticmethod
    def _dedupe_markdown_links(markdown: str) -> str:
        """Remove duplicate links while keeping readable labels."""
        seen: set[str] = set()

        def repl(match: re.Match[str]) -> str:
            label = match.group(1)
            url = match.group(2)
            if url in seen:
                return label
            seen.add(url)
            return match.group(0)

        return re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", repl, markdown)

    @staticmethod
    def _extract_domain(url: str) -> str:
        """Extract normalized domain (no www) for diversity balancing."""
        if not url:
            return ""
        try:
            netloc = urlparse(url).netloc.lower()
        except Exception:
            return ""
        return netloc[4:] if netloc.startswith("www.") else netloc

    @staticmethod
    def _extract_urls(markdown: str) -> set[str]:
        """Extract normalized HTTP(S) URLs from markdown/plain text."""
        raw_urls = re.findall(r"https?://[^\s)\]>]+", markdown)
        return {WriterExecutor._normalize_url(u) for u in raw_urls if u}

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Normalize URLs for robust inclusion checks."""
        trimmed = url.strip().rstrip(").,;!?")
        try:
            parsed = urlparse(trimmed)
        except Exception:
            return trimmed
        scheme = parsed.scheme.lower() or "https"
        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        path = parsed.path.rstrip("/")
        query = f"?{parsed.query}" if parsed.query else ""
        return f"{scheme}://{netloc}{path}{query}"

    @classmethod
    def _missing_items_for_markdown(cls, markdown: str, items: list[dict]) -> list[dict]:
        """Return selected items whose main URLs are missing from markdown."""
        present_urls = cls._extract_urls(markdown)
        missing: list[dict] = []
        for item in items:
            url = cls._normalize_url(str(item.get("url", "")))
            if url and url not in present_urls:
                missing.append(item)
        return missing

    @staticmethod
    def _append_missing_items_section(markdown: str, missing_items: list[dict]) -> str:
        """Append deterministic fallback lines for missing item URLs."""
        if not missing_items:
            return markdown

        lines = ["**Additional High-Signal Items**"]
        for item in missing_items:
            title = str(item.get("title") or "Untitled item").strip()
            summary = str(item.get("summary") or "").strip()
            summary = re.sub(r"\s+", " ", summary)
            if len(summary) > 180:
                summary = summary[:177].rstrip() + "..."
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            if summary:
                lines.append(
                    f"- [{title}]({url}) — {summary}; validate exposure and queue patch/detection checks."
                )
            else:
                lines.append(
                    f"- [{title}]({url}) — validate exposure and queue patch/detection checks."
                )

        if len(lines) == 1:
            return markdown
        return markdown.rstrip() + "\n\n" + "\n".join(lines)

    @staticmethod
    def _clip_markdown(markdown: str, limit: int) -> str:
        """Hard-cap markdown length, preferring paragraph boundaries."""
        if len(markdown) <= limit:
            return markdown
        split_at = markdown.rfind("\n\n", 0, limit)
        if split_at == -1:
            split_at = markdown.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        return markdown[:split_at].rstrip()

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
