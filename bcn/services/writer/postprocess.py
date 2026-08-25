"""Postprocessing helpers for writer-generated briefing bodies."""

from __future__ import annotations

import logging
import re
from typing import Any
from typing import Callable

from bcn.briefing import text as briefing_text
from bcn.common.config import Settings
from bcn.services.writer.models import PostprocessedBriefing
from bcn.services.writer.llm import WriterLLM

logger = logging.getLogger(__name__)


class WriterPostprocessor:
    """Apply deterministic cleanup and bounded enrichment to writer output."""

    def __init__(
        self,
        settings: Settings,
        *,
        writer_llm: WriterLLM,
        priority_score: Callable[[dict[str, Any]], float],
        char_limits: Callable[[str, int | None], tuple[int, int, int]],
    ) -> None:
        self.settings = settings
        self.writer_llm = writer_llm
        self.priority_score = priority_score
        self.char_limits = char_limits

    async def postprocess_briefing(
        self,
        *,
        briefing_body: str,
        selected_items: list[dict[str, Any]],
        mode: str,
        min_chars: int,
        target_chars: int,
        hard_max_chars: int,
    ) -> PostprocessedBriefing:
        """Enforce URL coverage and body length constraints on an LLM draft."""
        del min_chars, target_chars, hard_max_chars  # recomputed from current item count
        active_selected_items = list(selected_items)
        current_min_chars, current_target_chars, current_hard_max_chars = self.char_limits(
            mode,
            len(active_selected_items),
        )
        markdown = normalize_section_headings(
            dedupe_markdown_links((briefing_body or "").strip())
        )
        markdown = de_template_fields(markdown)

        for _ in range(2):
            missing_items = missing_items_for_markdown(markdown, active_selected_items)
            too_short = len(markdown) < current_min_chars
            if not missing_items and not too_short:
                break

            missing_urls = [
                str(item.get("url", "")) for item in missing_items if item.get("url")
            ]
            markdown = await self.writer_llm.enrich_briefing(
                draft_markdown=markdown,
                items=active_selected_items,
                min_chars=current_min_chars,
                target_chars=current_target_chars,
                hard_max_chars=current_hard_max_chars,
                missing_urls=missing_urls or None,
                mode=mode,
            )
            markdown = normalize_section_headings(
                dedupe_markdown_links(markdown.strip())
            )
            markdown = de_template_fields(markdown)

        missing_items = missing_items_for_markdown(markdown, active_selected_items)
        max_drops = max(0, int(self.settings.briefing_missing_coverage_max_drops))
        min_items_after_drop = max(
            1, int(self.settings.briefing_min_items_after_coverage_drop)
        )
        drops = 0
        while (
            missing_items
            and drops < max_drops
            and len(active_selected_items) > min_items_after_drop
        ):
            weakest = min(missing_items, key=lambda item: self.priority_score(item))
            active_selected_items = [
                item
                for item in active_selected_items
                if str(item.get("id")) != str(weakest.get("id"))
            ]
            current_min_chars, current_target_chars, current_hard_max_chars = self.char_limits(
                mode,
                len(active_selected_items),
            )
            drops += 1
            logger.warning(
                "Dropping uncovered low-priority item after rewrite retries: %s (%s)",
                weakest.get("title"),
                weakest.get("url"),
            )

            missing_items = missing_items_for_markdown(markdown, active_selected_items)
            if not missing_items:
                break

            markdown = await self.writer_llm.enrich_briefing(
                draft_markdown=markdown,
                items=active_selected_items,
                min_chars=current_min_chars,
                target_chars=current_target_chars,
                hard_max_chars=current_hard_max_chars,
                missing_urls=[
                    str(item.get("url", "")) for item in missing_items if item.get("url")
                ]
                or None,
                mode=mode,
            )
            markdown = normalize_section_headings(
                dedupe_markdown_links(markdown.strip())
            )
            markdown = de_template_fields(markdown)
            missing_items = missing_items_for_markdown(markdown, active_selected_items)

        if missing_items:
            logger.warning(
                "Coverage fallback appending %d missing selected item references.",
                len(missing_items),
            )
            markdown = append_missing_items_section(markdown, missing_items)
            markdown = normalize_section_headings(dedupe_markdown_links(markdown.strip()))
            markdown = de_template_fields(markdown)

        if len(markdown) > current_hard_max_chars:
            markdown = await self.writer_llm.tighten_briefing(
                markdown=markdown,
                target_chars=current_target_chars,
                hard_max_chars=current_hard_max_chars,
            )
            markdown = normalize_section_headings(dedupe_markdown_links(markdown.strip()))
            markdown = de_template_fields(markdown)

        if len(markdown) > current_hard_max_chars:
            markdown = clip_markdown(markdown, current_hard_max_chars)

        markdown = self.enforce_release_link_hygiene(
            markdown,
            active_selected_items,
            hard_max_chars=current_hard_max_chars,
        )
        return PostprocessedBriefing(
            markdown=markdown.strip(),
            selected_items=active_selected_items,
        )

    def enforce_release_link_hygiene(
        self,
        markdown: str,
        selected_items: list[dict[str, Any]],
        *,
        hard_max_chars: int,
    ) -> str:
        """Apply deterministic URL cleanup just before release checks."""
        cleaned = self.strip_unselected_markdown_links(markdown, selected_items)
        cleaned = normalize_section_headings(dedupe_markdown_links((cleaned or "").strip()))
        cleaned = de_template_fields(cleaned)

        # Enforce length before coverage: any URL a clip removes is restored
        # below as a reference, and the references budget is re-derived until
        # body plus references fit hard_max_chars together. Clips only shorten
        # the body, so the missing set only grows and the loop converges
        # within the selected-item count.
        if len(cleaned) > hard_max_chars:
            cleaned = clip_markdown(cleaned, hard_max_chars)
        missing_items = missing_items_for_markdown(cleaned, selected_items)
        for _ in range(len(selected_items) + 2):
            if not missing_items:
                break
            suffix_chars = len(append_missing_items_section("", missing_items))
            # The 400 floor avoids clip_markdown's mid-line fallback slicing a
            # URL in half; it can only engage far above the production item
            # cap, where a slight overflow beats a broken body.
            body_budget = max(400, hard_max_chars - suffix_chars)
            if len(cleaned) <= body_budget:
                break
            cleaned = clip_markdown(cleaned, body_budget)
            missing_items = missing_items_for_markdown(cleaned, selected_items)

        if missing_items:
            logger.warning(
                "Final deterministic coverage pass appending %d missing selected item references.",
                len(missing_items),
            )
            cleaned = append_missing_items_section(cleaned, missing_items)
            cleaned = normalize_section_headings(dedupe_markdown_links(cleaned.strip()))
            cleaned = de_template_fields(cleaned)

        return cleaned.strip()

    @staticmethod
    def strip_unselected_markdown_links(
        markdown: str,
        selected_items: list[dict[str, Any]],
    ) -> str:
        """Drop markdown-link formatting for any URL outside the selected item set."""
        selected_keys = {
            key
            for item in selected_items
            for key in briefing_text.item_url_keys(item)
        }
        selected_keys.discard("")

        def _replace(match: re.Match[str]) -> str:
            label = match.group(1)
            url = match.group(2)
            key = briefing_text.canonical_url_key(url)
            if key and key not in selected_keys:
                return label
            return match.group(0)

        return re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", _replace, markdown or "")

    @staticmethod
    def strip_unselected_github_advisory_links(
        markdown: str,
        selected_items: list[dict[str, Any]],
    ) -> str:
        """Drop markdown-link formatting for GHSA URLs that were not selected."""
        selected_urls = {
            briefing_text.normalize_url(str(item.get("url", "")))
            for item in selected_items
            if item.get("url")
        }

        def _replace(match: re.Match[str]) -> str:
            label = match.group(1)
            url = match.group(2)
            normalized = briefing_text.normalize_url(url)
            lowered = normalized.lower()
            is_github_ghsa = "github.com/" in lowered and "/ghsa-" in lowered
            if is_github_ghsa and normalized not in selected_urls:
                return label
            return match.group(0)

        return re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", _replace, markdown or "")


def dedupe_markdown_links(markdown: str) -> str:
    """Remove duplicate markdown links while preserving surrounding text."""
    return briefing_text.dedupe_markdown_links(markdown)


def normalize_section_headings(markdown: str) -> str:
    """Normalize digest section headings to the house style."""
    return briefing_text.normalize_section_headings(markdown)


def de_template_fields(markdown: str) -> str:
    """Strip template filler fields left by the model."""
    return briefing_text.de_template_fields(markdown)


def missing_items_for_markdown(markdown: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return selected items whose URLs are missing from markdown."""
    return briefing_text.missing_items_for_markdown(markdown, items)


def append_missing_items_section(markdown: str, missing_items: list[dict[str, Any]]) -> str:
    """Append a deterministic references section for missing URLs."""
    return briefing_text.append_missing_items_section(markdown, missing_items)


def clip_markdown(markdown: str, limit: int) -> str:
    """Clip markdown to a hard size limit while preserving readability."""
    return briefing_text.clip_markdown(markdown, limit)


__all__ = [
    "WriterPostprocessor",
    "append_missing_items_section",
    "clip_markdown",
    "de_template_fields",
    "dedupe_markdown_links",
    "missing_items_for_markdown",
    "normalize_section_headings",
]
