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

from bcn.briefing import BriefingFactVerifier, BriefingQualityGate, BriefingSelector
from bcn.briefing import text as briefing_text
from bcn.comfyui import ComfyUIClient
from bcn.config import Settings
from bcn.db import (
    get_analyzed_items,
    get_recent_briefings,
    get_recent_published_items,
    insert_briefing,
)
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
        self.selector = BriefingSelector(settings)
        self.quality = BriefingQualityGate(settings)
        self.verifier = BriefingFactVerifier(settings, llm=self.llm)

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

        item_dicts = [dict(i) for i in items]
        if bool(self.settings.briefing_skip_if_no_high_signal):
            high_signal = self.selector.high_signal_count(item_dicts)
            min_high_signal = max(1, int(self.settings.briefing_min_high_signal_to_publish))
            if high_signal < min_high_signal:
                msg = (
                    "Quiet day — not enough high-signal items "
                    f"({high_signal} < {min_high_signal}). Skipping briefing."
                )
                logger.info(msg)
                event_queue.enqueue_event(new_agent_text_message(msg))
                return

        recent_published = await get_recent_published_items(
            hours=self.settings.briefing_novelty_lookback_hours,
            limit=self.settings.briefing_novelty_max_items,
        )
        quiet_mode = self._is_quiet_day(item_dicts)
        mode = "quiet_day" if quiet_mode else "standard"

        selected_items = self._select_items_for_briefing(
            item_dicts,
            recent_published=[dict(r) for r in recent_published],
            quiet_mode=quiet_mode,
        )
        if not selected_items:
            msg = "No items remained after quality/diversity filtering. Skipping briefing."
            logger.info(msg)
            event_queue.enqueue_event(new_agent_text_message(msg))
            return
        min_chars, target_chars, hard_max_chars = self._char_limits(
            mode,
            selected_count=len(selected_items),
        )

        history = await get_recent_briefings(limit=self.settings.briefing_history_items)
        history_items = [dict(r) for r in history]

        briefing_body = await self.llm.generate_briefing(
            selected_items,
            recent_briefings=history_items,
            mode=mode,
        )
        briefing_body = await self._postprocess_briefing(
            briefing_body=briefing_body,
            selected_items=selected_items,
            mode=mode,
            min_chars=min_chars,
            target_chars=target_chars,
            hard_max_chars=hard_max_chars,
        )

        max_rewrites = max(0, int(self.settings.briefing_critique_max_rounds))
        rewrites = 0
        release_passed = False
        last_gate: dict[str, object] = {}
        last_critique: dict[str, object] = {}
        last_verification: dict[str, object] = {
            "passed": True,
            "score": 100,
            "issues": [],
            "recommendations": [],
        }
        while True:
            last_gate = self._quality_gate(
                markdown=briefing_body,
                selected_items=selected_items,
                mode=mode,
                min_chars=min_chars,
                hard_max_chars=hard_max_chars,
            )
            if self.settings.briefing_critique_enabled:
                last_critique = await self.llm.critique_briefing(
                    draft_markdown=briefing_body,
                    items=selected_items,
                    mode=mode,
                    gate_hard_issues=[str(i) for i in last_gate.get("hard_issues", [])],
                    gate_soft_issues=[str(i) for i in last_gate.get("soft_issues", [])],
                )
            else:
                last_critique = {
                    "passed": True,
                    "score": 100,
                    "dimension_scores": {
                        "actionability": 100,
                        "source_diversity": 100,
                        "link_hygiene": 100,
                        "clarity": 100,
                        "style": 100,
                    },
                    "issues": [],
                    "recommendations": [],
                }

            if self.settings.briefing_verifier_enabled:
                last_verification = await self.verifier.evaluate(
                    briefing_body,
                    selected_items,
                    mode=mode,
                )

            gate_passed = bool(last_gate.get("passed", False))
            critique_passed = self._passes_critic_thresholds(last_critique)
            verification_passed = bool(last_verification.get("passed", True))
            release_passed = gate_passed and critique_passed and verification_passed
            if release_passed:
                logger.info(
                    "Briefing approved after %d rewrite(s) (critic_score=%s verifier_score=%s)",
                    rewrites,
                    last_critique.get("score"),
                    last_verification.get("score"),
                )
                break

            if rewrites >= max_rewrites:
                break

            feedback: list[str] = []
            feedback.extend([str(i) for i in last_gate.get("issues", [])])
            feedback.extend([str(i) for i in last_critique.get("issues", [])])
            feedback.extend([str(i) for i in last_critique.get("recommendations", [])])
            feedback.extend([str(i) for i in last_verification.get("issues", [])])
            feedback.extend([str(i) for i in last_verification.get("recommendations", [])])

            rewrites += 1
            logger.info(
                "Briefing failed release checks; rewrite %d/%d "
                "(gate=%s critique=%s verifier=%s score=%s verifier_score=%s)",
                rewrites,
                max_rewrites,
                gate_passed,
                critique_passed,
                verification_passed,
                last_critique.get("score"),
                last_verification.get("score"),
            )
            briefing_body = await self.llm.revise_briefing(
                draft_markdown=briefing_body,
                items=selected_items,
                feedback=feedback,
                mode=mode,
                min_chars=min_chars,
                target_chars=target_chars,
                hard_max_chars=hard_max_chars,
            )
            briefing_body = await self._postprocess_briefing(
                briefing_body=briefing_body,
                selected_items=selected_items,
                mode=mode,
                min_chars=min_chars,
                target_chars=target_chars,
                hard_max_chars=hard_max_chars,
            )

        if not release_passed:
            msg = (
                "Blocking publish: briefing did not meet release thresholds after "
                f"{rewrites} rewrite(s). gate={bool(last_gate.get('passed', False))} "
                f"critic={self._passes_critic_thresholds(last_critique)} "
                f"verifier={bool(last_verification.get('passed', True))}"
            )
            logger.warning(msg)
            event_queue.enqueue_event(new_agent_text_message(msg))
            return

        briefing_body = self._normalize_section_headings(briefing_body)
        briefing_body = self._de_template_fields(briefing_body)

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

    def _select_items_for_briefing(
        self,
        items: list[dict],
        recent_published: list[dict] | None = None,
        *,
        quiet_mode: bool = False,
    ) -> list[dict]:
        return self.selector.select_items(
            items=items,
            recent_published=recent_published,
            quiet_mode=quiet_mode,
        )

    def _priority_score(
        self,
        item: dict,
        recent_published: list[dict] | None = None,
    ) -> float:
        return self.selector.priority_score(item, recent_published)

    def _passes_source_floor(self, item: dict) -> bool:
        return self.selector.passes_source_floor(item)

    def _is_quiet_day(self, items: list[dict]) -> bool:
        return self.selector.is_quiet_day(items)

    def _char_limits(self, mode: str, selected_count: int | None = None) -> tuple[int, int, int]:
        min_chars, target_chars, hard_max_chars = self.quality.char_limits(mode)
        if selected_count is not None and selected_count <= 1:
            min_chars = min(min_chars, int(self.settings.briefing_single_item_min_chars))
            target_chars = min(target_chars, int(self.settings.briefing_single_item_target_chars))
            hard_max_chars = min(
                hard_max_chars,
                int(self.settings.briefing_single_item_hard_max_chars),
            )
            target_chars = max(min_chars, target_chars)
            hard_max_chars = max(target_chars, hard_max_chars)
        return min_chars, target_chars, hard_max_chars

    def _quality_gate(
        self,
        markdown: str,
        selected_items: list[dict],
        mode: str,
        min_chars: int,
        hard_max_chars: int,
    ) -> dict[str, object]:
        return self.quality.evaluate(
            markdown=markdown,
            selected_items=selected_items,
            mode=mode,
            min_chars=min_chars,
            hard_max_chars=hard_max_chars,
        )

    def _passes_critic_thresholds(self, critique: dict[str, object]) -> bool:
        """Apply blocking thresholds for critic score and key dimensions."""
        if not critique:
            return False
        if not bool(critique.get("passed", False)):
            return False

        score = int(critique.get("score", 0) or 0)
        dims = critique.get("dimension_scores", {}) or {}
        if not isinstance(dims, dict):
            dims = {}
        actionability = int(dims.get("actionability", 0) or 0)
        source_diversity = int(dims.get("source_diversity", 0) or 0)
        link_hygiene = int(dims.get("link_hygiene", 0) or 0)

        return (
            score >= int(self.settings.briefing_critic_min_score)
            and actionability >= int(self.settings.briefing_critic_min_actionability)
            and source_diversity >= int(self.settings.briefing_critic_min_source_diversity)
            and link_hygiene >= int(self.settings.briefing_critic_min_link_hygiene)
        )

    async def _postprocess_briefing(
        self,
        briefing_body: str,
        selected_items: list[dict],
        mode: str,
        min_chars: int,
        target_chars: int,
        hard_max_chars: int,
    ) -> str:
        """Enforce URL coverage and depth/length constraints on LLM draft."""
        markdown = self._normalize_section_headings(
            self._dedupe_markdown_links((briefing_body or "").strip())
        )
        markdown = self._de_template_fields(markdown)

        for _ in range(2):
            missing_items = self._missing_items_for_markdown(markdown, selected_items)
            too_short = len(markdown) < min_chars
            if not missing_items and not too_short:
                break

            missing_urls = [str(i.get("url", "")) for i in missing_items if i.get("url")]
            markdown = await self.llm.enrich_briefing(
                draft_markdown=markdown,
                items=selected_items,
                min_chars=min_chars,
                target_chars=target_chars,
                hard_max_chars=hard_max_chars,
                missing_urls=missing_urls or None,
                mode=mode,
            )
            markdown = self._normalize_section_headings(
                self._dedupe_markdown_links(markdown.strip())
            )
            markdown = self._de_template_fields(markdown)

        missing_items = self._missing_items_for_markdown(markdown, selected_items)
        max_drops = max(0, int(self.settings.briefing_missing_coverage_max_drops))
        min_items_after_drop = max(1, int(self.settings.briefing_min_items_after_coverage_drop))
        drops = 0
        while missing_items and drops < max_drops and len(selected_items) > min_items_after_drop:
            weakest = min(
                missing_items,
                key=lambda item: self._priority_score(item),
            )
            selected_items[:] = [
                item for item in selected_items
                if str(item.get("id")) != str(weakest.get("id"))
            ]
            drops += 1
            logger.warning(
                "Dropping uncovered low-priority item after rewrite retries: %s (%s)",
                weakest.get("title"),
                weakest.get("url"),
            )

            markdown = await self.llm.enrich_briefing(
                draft_markdown=markdown,
                items=selected_items,
                min_chars=min_chars,
                target_chars=target_chars,
                hard_max_chars=hard_max_chars,
                missing_urls=[str(i.get("url", "")) for i in missing_items if i.get("url")] or None,
                mode=mode,
            )
            markdown = self._normalize_section_headings(
                self._dedupe_markdown_links(markdown.strip())
            )
            markdown = self._de_template_fields(markdown)
            missing_items = self._missing_items_for_markdown(markdown, selected_items)

        if len(markdown) > hard_max_chars:
            markdown = await self.llm.tighten_briefing(
                markdown=markdown,
                target_chars=target_chars,
                hard_max_chars=hard_max_chars,
            )
            markdown = self._normalize_section_headings(
                self._dedupe_markdown_links(markdown.strip())
            )
            markdown = self._de_template_fields(markdown)

        if len(markdown) > hard_max_chars:
            markdown = self._clip_markdown(markdown, hard_max_chars)

        return markdown.strip()

    @staticmethod
    def _dedupe_markdown_links(markdown: str) -> str:
        return briefing_text.dedupe_markdown_links(markdown)

    @staticmethod
    def _normalize_section_headings(markdown: str) -> str:
        return briefing_text.normalize_section_headings(markdown)

    @staticmethod
    def _de_template_fields(markdown: str) -> str:
        return briefing_text.de_template_fields(markdown)

    @staticmethod
    def _missing_items_for_markdown(markdown: str, items: list[dict]) -> list[dict]:
        return briefing_text.missing_items_for_markdown(markdown, items)

    @staticmethod
    def _append_missing_items_section(markdown: str, missing_items: list[dict]) -> str:
        return briefing_text.append_missing_items_section(markdown, missing_items)

    @staticmethod
    def _clip_markdown(markdown: str, limit: int) -> str:
        return briefing_text.clip_markdown(markdown, limit)

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
        """Wrap the briefing body with an optional cover image in Markdown."""
        md = ""
        if cover_url:
            md += f"![Daily Cover]({cover_url})\n\n"
        md += briefing_body
        return md

    @staticmethod
    def _format_html(briefing_body: str, cover_url: str) -> str:
        """Convert the briefing body to basic HTML."""
        html_body = briefing_body
        html_body = re.sub(r"^### (.+)$", r"<h3>\1</h3>", html_body, flags=re.MULTILINE)
        html_body = re.sub(r"^## (.+)$", r"<h2>\1</h2>", html_body, flags=re.MULTILINE)
        html_body = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html_body)
        html_body = re.sub(r"\*(.+?)\*", r"<em>\1</em>", html_body)
        html_body = re.sub(r"\[([^\]]+)]\(([^)]+)\)", r'<a href="\2">\1</a>', html_body)
        html_body = re.sub(r"\n{2,}", "</p>\n<p>", html_body)
        html_body = f"<p>{html_body}</p>"

        parts = ["<html><body>"]
        if cover_url:
            parts.append(
                f'<img src="{cover_url}" alt="Daily Cover" style="max-width:600px"/>'
            )
        parts.append(html_body)
        parts.append("</body></html>")
        return "\n".join(parts)
