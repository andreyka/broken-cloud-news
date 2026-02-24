"""Writer agent: generates daily briefings with cover images."""

from __future__ import annotations

import logging
import re
import subprocess
import time
from typing import Any
from uuid import UUID

from typing_extensions import override

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import AgentSkill
from a2a.utils import new_agent_text_message

from bcn.agents.base import enqueue_event_safe
from bcn.briefing import BriefingFactVerifier, BriefingQualityGate, BriefingSelector
from bcn.briefing import text as briefing_text
from bcn.comfyui import ComfyUIClient
from bcn.config import Settings
from bcn.db import (
    append_generation_round,
    create_generation_run,
    finalize_generation_run,
    get_analyzed_items,
    insert_generation_preference_pair,
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
            await enqueue_event_safe(event_queue, new_agent_text_message(msg))
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
                await enqueue_event_safe(event_queue, new_agent_text_message(msg))
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
            await enqueue_event_safe(event_queue, new_agent_text_message(msg))
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

        trace_run_id = await self._trace_start_run(
            mode=mode,
            selected_items=selected_items,
            initial_draft=briefing_body,
        )

        max_rewrites = max(0, int(self.settings.briefing_critique_max_rounds))
        rewrites = 0
        trace_round_index = 0
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
            briefing_body = self._enforce_release_link_hygiene(
                briefing_body,
                selected_items,
                hard_max_chars=hard_max_chars,
            )
            round_input = briefing_body
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

            feedback: list[str] = []
            rewritten_output: str | None = None

            if release_passed:
                logger.info(
                    "Briefing approved after %d rewrite(s) (critic_score=%s verifier_score=%s)",
                    rewrites,
                    last_critique.get("score"),
                    last_verification.get("score"),
                )
                await self._trace_round(
                    run_id=trace_run_id,
                    round_index=trace_round_index,
                    phase="initial" if trace_round_index == 0 else "rewrite",
                    draft_input=round_input,
                    gate_result=last_gate,
                    critique_result=last_critique,
                    verifier_result=last_verification,
                    feedback=feedback,
                    rewrite_output=rewritten_output,
                    passed=True,
                )
                break

            if rewrites >= max_rewrites:
                await self._trace_round(
                    run_id=trace_run_id,
                    round_index=trace_round_index,
                    phase="initial" if trace_round_index == 0 else "rewrite",
                    draft_input=round_input,
                    gate_result=last_gate,
                    critique_result=last_critique,
                    verifier_result=last_verification,
                    feedback=feedback,
                    rewrite_output=rewritten_output,
                    passed=False,
                )
                break

            feedback.extend([str(i) for i in last_gate.get("issues", [])])
            feedback.extend([str(i) for i in last_critique.get("issues", [])])
            feedback.extend([str(i) for i in last_critique.get("recommendations", [])])
            feedback.extend([str(i) for i in last_verification.get("issues", [])])
            feedback.extend([str(i) for i in last_verification.get("recommendations", [])])

            # If coverage keeps regressing, inject deterministic fallback links
            # before asking the model to rewrite again.
            missing_items = self._missing_items_for_markdown(briefing_body, selected_items)
            if missing_items:
                logger.warning(
                    "URL coverage regression detected before rewrite; appending %d missing references.",
                    len(missing_items),
                )
                briefing_body = self._append_missing_items_section(briefing_body, missing_items)
                briefing_body = self._normalize_section_headings(
                    self._dedupe_markdown_links(briefing_body.strip())
                )
                briefing_body = self._de_template_fields(briefing_body)

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
            rewritten_output = await self.llm.revise_briefing(
                draft_markdown=briefing_body,
                items=selected_items,
                feedback=feedback,
                mode=mode,
                min_chars=min_chars,
                target_chars=target_chars,
                hard_max_chars=hard_max_chars,
            )
            rewritten_output = await self._postprocess_briefing(
                briefing_body=rewritten_output,
                selected_items=selected_items,
                mode=mode,
                min_chars=min_chars,
                target_chars=target_chars,
                hard_max_chars=hard_max_chars,
            )
            await self._trace_round(
                run_id=trace_run_id,
                round_index=trace_round_index,
                phase="initial" if trace_round_index == 0 else "rewrite",
                draft_input=round_input,
                gate_result=last_gate,
                critique_result=last_critique,
                verifier_result=last_verification,
                feedback=feedback,
                rewrite_output=rewritten_output,
                passed=False,
            )
            await self._trace_preference_pair(
                run_id=trace_run_id,
                round_index=trace_round_index + 1,
                chosen_text=rewritten_output,
                rejected_text=round_input,
                rationale=self._build_rationale(feedback),
            )
            briefing_body = rewritten_output
            trace_round_index += 1

        if not release_passed:
            msg = (
                "Blocking publish: briefing did not meet release thresholds after "
                f"{rewrites} rewrite(s). gate={bool(last_gate.get('passed', False))} "
                f"critic={self._passes_critic_thresholds(last_critique)} "
                f"verifier={bool(last_verification.get('passed', True))}"
            )
            logger.warning(msg)
            await self._trace_finalize_run(
                run_id=trace_run_id,
                decision="BLOCKED",
                decision_reason=msg,
                rewrite_count=rewrites,
                final_draft=briefing_body,
                final_gate=last_gate,
                final_critique=last_critique,
                final_verifier=last_verification,
                briefing_id=None,
            )
            await enqueue_event_safe(event_queue, new_agent_text_message(msg))
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
        await self._trace_finalize_run(
            run_id=trace_run_id,
            decision="PUBLISHED",
            decision_reason="release_checks_passed",
            rewrite_count=rewrites,
            final_draft=briefing_body,
            final_gate=last_gate,
            final_critique=last_critique,
            final_verifier=last_verification,
            briefing_id=briefing_id,
        )

        msg = f"Briefing {briefing_id} created with {len(selected_items)} items"
        logger.info(msg)
        await enqueue_event_safe(event_queue, new_agent_text_message(msg))

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

            # Recompute after the drop so we don't pass stale URLs to the enricher.
            missing_items = self._missing_items_for_markdown(markdown, selected_items)
            if not missing_items:
                break

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

        # Final deterministic fallback to prevent endless URL coverage oscillation.
        if missing_items:
            logger.warning(
                "Coverage fallback appending %d missing selected item references.",
                len(missing_items),
            )
            markdown = self._append_missing_items_section(markdown, missing_items)
            markdown = self._normalize_section_headings(
                self._dedupe_markdown_links(markdown.strip())
            )
            markdown = self._de_template_fields(markdown)

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

        markdown = self._enforce_release_link_hygiene(
            markdown,
            selected_items,
            hard_max_chars=hard_max_chars,
        )
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

    def _enforce_release_link_hygiene(
        self,
        markdown: str,
        selected_items: list[dict],
        *,
        hard_max_chars: int,
    ) -> str:
        """Apply deterministic URL cleanup just before release checks."""
        cleaned = self._strip_unselected_github_advisory_links(markdown, selected_items)
        cleaned = self._normalize_section_headings(
            self._dedupe_markdown_links((cleaned or "").strip())
        )
        cleaned = self._de_template_fields(cleaned)

        missing_items = self._missing_items_for_markdown(cleaned, selected_items)
        if missing_items:
            logger.warning(
                "Final deterministic coverage pass appending %d missing selected item references.",
                len(missing_items),
            )
            cleaned = self._append_missing_items_section(cleaned, missing_items)
            cleaned = self._normalize_section_headings(
                self._dedupe_markdown_links(cleaned.strip())
            )
            cleaned = self._de_template_fields(cleaned)

        if len(cleaned) > hard_max_chars:
            cleaned = self._clip_markdown(cleaned, hard_max_chars)

        return cleaned.strip()

    @staticmethod
    def _strip_unselected_github_advisory_links(markdown: str, selected_items: list[dict]) -> str:
        """Drop markdown-link formatting for GHSA advisory URLs not in selected items."""
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

    async def _trace_start_run(
        self,
        *,
        mode: str,
        selected_items: list[dict[str, Any]],
        initial_draft: str,
    ) -> UUID | None:
        ids: list[UUID] = []
        for item in selected_items:
            item_id = self._coerce_uuid(item.get("id"))
            if item_id:
                ids.append(item_id)

        try:
            return await create_generation_run(
                trigger_source="writer",
                mode=mode,
                selected_item_ids=ids,
                selected_items=selected_items,
                llm_model=self.settings.llm_model,
                llm_model_version=self._model_version(),
                prompts=self.llm.prompt_versions(),
                config_snapshot=self._component_config_snapshot(),
                git_sha=self._git_sha(),
                initial_draft=initial_draft,
            )
        except Exception:
            logger.exception("Failed to persist generation trace start")
            return None

    async def _trace_round(
        self,
        *,
        run_id: UUID | None,
        round_index: int,
        phase: str,
        draft_input: str,
        gate_result: dict[str, object] | None,
        critique_result: dict[str, object] | None,
        verifier_result: dict[str, object] | None,
        feedback: list[str] | None,
        rewrite_output: str | None,
        passed: bool,
    ) -> None:
        if not run_id:
            return
        try:
            await append_generation_round(
                run_id=run_id,
                round_index=round_index,
                phase=phase,
                draft_input=draft_input,
                gate_result=self._to_dict(gate_result),
                critique_result=self._to_dict(critique_result),
                verifier_result=self._to_dict(verifier_result),
                feedback=[str(item) for item in (feedback or [])],
                rewrite_output=rewrite_output,
                passed=passed,
            )
        except Exception:
            logger.exception("Failed to persist generation trace round")

    async def _trace_preference_pair(
        self,
        *,
        run_id: UUID | None,
        round_index: int,
        chosen_text: str,
        rejected_text: str,
        rationale: str | None,
    ) -> None:
        if not run_id:
            return
        try:
            await insert_generation_preference_pair(
                run_id=run_id,
                round_index=round_index,
                chosen_text=chosen_text,
                rejected_text=rejected_text,
                rationale=rationale,
                source="auto_writer_loop",
            )
        except Exception:
            logger.exception("Failed to persist generation preference pair")

    async def _trace_finalize_run(
        self,
        *,
        run_id: UUID | None,
        decision: str,
        decision_reason: str | None,
        rewrite_count: int,
        final_draft: str | None,
        final_gate: dict[str, object] | None,
        final_critique: dict[str, object] | None,
        final_verifier: dict[str, object] | None,
        briefing_id: UUID | None,
    ) -> None:
        if not run_id:
            return
        try:
            await finalize_generation_run(
                run_id=run_id,
                decision=decision,
                decision_reason=decision_reason,
                rewrite_count=rewrite_count,
                final_draft=final_draft,
                final_gate=self._to_dict(final_gate),
                final_critique=self._to_dict(final_critique),
                final_verifier=self._to_dict(final_verifier),
                briefing_id=briefing_id,
            )
        except Exception:
            logger.exception("Failed to finalize generation trace")

    @staticmethod
    def _coerce_uuid(value: object) -> UUID | None:
        if isinstance(value, UUID):
            return value
        if isinstance(value, str):
            try:
                return UUID(value)
            except (TypeError, ValueError):
                return None
        return None

    @staticmethod
    def _to_dict(value: dict[str, object] | None) -> dict[str, Any]:
        if isinstance(value, dict):
            return {str(k): v for k, v in value.items()}
        return {}

    @staticmethod
    def _build_rationale(feedback: list[str]) -> str:
        points = [str(item).strip() for item in feedback if str(item).strip()]
        return " | ".join(points[:6])[:2000]

    def _model_version(self) -> str:
        model = (self.settings.llm_model or "").strip()
        if ":" in model:
            return model.rsplit(":", 1)[-1].strip() or "unknown"
        if "@" in model:
            return model.rsplit("@", 1)[-1].strip() or "unknown"
        return "unknown"

    def _component_config_snapshot(self) -> dict[str, Any]:
        raw = self.settings.model_dump()
        filtered: dict[str, Any] = {}
        for key, value in raw.items():
            lowered = key.lower()
            if (
                any(secret in lowered for secret in ("token", "password", "webhook"))
                or lowered == "database_url"
                or lowered.startswith("smtp_")
            ):
                continue
            filtered[key] = value

        collector_keys = tuple(prefix for prefix in ("ghsa_", "rss_", "reddit_", "twitter_", "scrape_"))
        writer_keys = tuple(prefix for prefix in ("briefing_", "telegram_overflow_mode"))
        collector = {
            key: filtered[key]
            for key in filtered
            if key.startswith(collector_keys)
        }
        analyzer = {
            key: filtered[key]
            for key in filtered
            if key.startswith("scrape_")
            or key in {"relevance_threshold", "llm_model", "llm_timeout", "llm_base_url"}
        }
        writer = {
            key: filtered[key]
            for key in filtered
            if key.startswith(writer_keys)
            or key in {
                "relevance_threshold",
                "briefing_lookback_hours",
                "llm_model",
                "llm_timeout",
                "llm_base_url",
                "comfyui_url",
                "comfyui_timeout",
                "comfyui_poll_interval",
            }
        }
        critic = {
            key: filtered[key]
            for key in filtered
            if key.startswith("briefing_critic_") or key in {"briefing_gate_mode"}
        }
        verifier = {
            key: filtered[key]
            for key in filtered
            if key.startswith("briefing_verifier_")
        }

        return {
            "collector": collector,
            "analyzer": analyzer,
            "writer": writer,
            "critic": critic,
            "verifier": verifier,
        }

    @staticmethod
    def _git_sha() -> str | None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
                timeout=3,
            )
            sha = (result.stdout or "").strip()
            return sha or None
        except Exception:
            return None

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
