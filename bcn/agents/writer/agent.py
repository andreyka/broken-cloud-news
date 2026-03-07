"""Writer agent: generates daily briefings with cover images."""

from __future__ import annotations

import logging
import subprocess
import time
from typing import Any
from uuid import UUID

from a2a.server.agent_execution import AgentExecutor
from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from a2a.types import AgentSkill
from a2a.utils import new_agent_text_message
from typing_extensions import override

from bcn.agents.base import enqueue_event_safe
from bcn.agents.writer.service import WriterService
from bcn.common.config import Settings
from bcn.common.db import append_generation_round
from bcn.common.db import create_generation_run
from bcn.common.db import finalize_stale_pending_generation_runs
from bcn.common.db import finalize_generation_run
from bcn.common.db import get_analyzed_items
from bcn.common.db import get_recent_briefings
from bcn.common.db import get_recent_published_items
from bcn.common.db import get_top_items_for_period
from bcn.common.db import insert_briefing
from bcn.common.db import insert_generation_preference_pair
from bcn.common.db import release_items_from_writing
from bcn.workflows.modes import ALL_MODES
from bcn.workflows.modes import REGULAR_DAILY_BRIEFING_MODE
from bcn.workflows.modes import REGULAR_MONTHLY_NEWSLETTER_MODE
from bcn.workflows.modes.common import render_writer_handoff_payload

logger = logging.getLogger(__name__)
_SUPPORTED_WORKFLOW_MODES = frozenset(ALL_MODES)

SKILLS = [
    AgentSkill(
        id="generate_briefing",
        name="Generate Briefing",
        description=(
            "Generate a security briefing with cover image. "
            "Supports mode suffixes: regular_daily_briefing, ad_hoc, regular_monthly_newsletter."
        ),
        tags=["briefing", "writer"],
        examples=[
            "write",
            "generate_briefing",
            "generate_briefing::regular_daily_briefing",
            "generate_briefing::ad_hoc",
            "generate_briefing::regular_monthly_newsletter",
        ],
    ),
]


class WriterExecutor(AgentExecutor):
    """A2A agent that composes briefings from top-scored items."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.service = WriterService(settings)
        self.llm_client = self.service.llm_client
        self.writer_llm = self.service.writer_llm
        self.critic_llm = self.service.critic_llm
        self.comfyui = self.service.comfyui
        self.selector = self.service.selector
        self.quality = self.service.quality
        self.verifier = self.service.verifier

    @override
    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Generate a briefing from top-scored items and store it as DRAFT."""
        workflow_mode = self._resolve_workflow_mode(context.get_user_input() or "")
        claimed_item_ids: list[UUID] = []
        stale_minutes = int(
            getattr(self.settings, "generation_run_stale_pending_minutes", 180)
        )
        if stale_minutes > 0:
            try:
                finalized = await finalize_stale_pending_generation_runs(
                    max_age_minutes=max(1, stale_minutes),
                    decision="BLOCKED",
                    decision_reason="writer_auto_finalize_stale_pending_run",
                )
                if finalized:
                    logger.warning(
                        "Auto-finalized %d stale PENDING generation runs before writer execution",
                        finalized,
                    )
            except Exception:
                logger.exception("Failed to auto-finalize stale PENDING generation runs")

        try:
            if workflow_mode == REGULAR_MONTHLY_NEWSLETTER_MODE:
                items = await get_top_items_for_period(
                    days=max(1, int(self.settings.monthly_newsletter_lookback_days)),
                    min_score=max(1, int(self.settings.monthly_newsletter_min_score)),
                    limit=max(
                        int(self.settings.monthly_newsletter_max_items) * 4,
                        int(self.settings.monthly_newsletter_min_items),
                    ),
                )
            else:
                claim_limit = max(int(self.settings.briefing_max_items) * 8, 40)
                items = await get_analyzed_items(
                    min_score=self.settings.relevance_threshold,
                    hours=self.settings.briefing_lookback_hours,
                    limit=claim_limit,
                )
                claimed_item_ids = [
                    item_id
                    for item_id in (
                        self._coerce_uuid(item.get("id"))
                        for item in items
                        if str(item.get("status", "")).upper() == "WRITING"
                    )
                    if item_id is not None
                ]

            if not items:
                if workflow_mode == REGULAR_MONTHLY_NEWSLETTER_MODE:
                    msg = (
                        "Monthly newsletter skipped: no high-signal items found "
                        f"in last {self.settings.monthly_newsletter_lookback_days} days."
                    )
                else:
                    msg = (
                        f"Quiet day — no items scored >= {self.settings.relevance_threshold} "
                        f"in the last {self.settings.briefing_lookback_hours}h. "
                        "Skipping briefing."
                    )
                logger.info(msg)
                await enqueue_event_safe(
                    event_queue,
                    new_agent_text_message(
                        self._compose_handoff_message(
                            workflow_mode=workflow_mode,
                            decision="skip",
                            item_count=0,
                            human_message=msg,
                        )
                    ),
                )
                return

            item_dicts = [dict(i) for i in items]
            await self._execute_core(
                item_dicts=item_dicts,
                event_queue=event_queue,
                workflow_mode=workflow_mode,
            )
        finally:
            if claimed_item_ids:
                try:
                    await release_items_from_writing(claimed_item_ids)
                except Exception:
                    logger.exception(
                        "Failed to release %d WRITING items after writer run",
                        len(claimed_item_ids),
                    )

    async def close(self) -> None:
        """Release writer resources."""
        await self.service.close()

    @staticmethod
    def _compose_handoff_message(
        *,
        workflow_mode: str,
        decision: str,
        briefing_id: UUID | None = None,
        item_count: int | None = None,
        human_message: str = "",
    ) -> str:
        """Compose one payload containing contract JSON and human-readable text."""
        payload = render_writer_handoff_payload(
            mode=workflow_mode,
            decision=decision,
            briefing_id=briefing_id,
            item_count=item_count,
        )
        text = str(human_message or "").strip()
        return payload if not text else f"{payload}\n{text}"

    async def _execute_core(
        self,
        item_dicts: list[dict],
        event_queue: EventQueue,
        workflow_mode: str,
    ) -> None:
        """Core briefing generation logic, separated for resource cleanup."""
        if workflow_mode == REGULAR_MONTHLY_NEWSLETTER_MODE:
            mode = "monthly_newsletter"
            selected_items = self._select_items_for_monthly_newsletter(item_dicts)
        else:
            if bool(self.settings.briefing_skip_if_no_high_signal):
                high_signal = self.selector.high_signal_count(item_dicts)
                min_high_signal = max(
                    1, int(self.settings.briefing_min_high_signal_to_publish)
                )
                if high_signal < min_high_signal:
                    msg = (
                        "Quiet day — not enough high-signal items "
                        f"({high_signal} < {min_high_signal}). Skipping briefing."
                    )
                    logger.info(msg)
                    await enqueue_event_safe(
                        event_queue,
                        new_agent_text_message(
                            self._compose_handoff_message(
                                workflow_mode=workflow_mode,
                                decision="skip",
                                item_count=0,
                                human_message=msg,
                            )
                        ),
                    )
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
            if workflow_mode == REGULAR_MONTHLY_NEWSLETTER_MODE:
                msg = (
                    "Monthly newsletter skipped: not enough diverse high-signal items "
                    "after selection constraints."
                )
            else:
                msg = (
                    "No items remained after quality/diversity filtering. "
                    "Skipping briefing."
                )
            logger.info(msg)
            await enqueue_event_safe(
                event_queue,
                new_agent_text_message(
                    self._compose_handoff_message(
                        workflow_mode=workflow_mode,
                        decision="skip",
                        item_count=0,
                        human_message=msg,
                    )
                ),
            )
            return
        min_chars, target_chars, hard_max_chars = self._char_limits(
            mode,
            selected_count=len(selected_items),
        )

        history = await get_recent_briefings(limit=self.settings.briefing_history_items)
        history_items = [dict(r) for r in history]

        briefing_body = await self.writer_llm.generate_briefing(
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
        min_chars, target_chars, hard_max_chars = self._char_limits(
            mode,
            selected_count=len(selected_items),
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
        trace_finalized = False
        try:
            while True:
                min_chars, target_chars, hard_max_chars = self._char_limits(
                    mode,
                    selected_count=len(selected_items),
                )
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
                    last_critique = await self.critic_llm.critique_briefing(
                        draft_markdown=briefing_body,
                        items=selected_items,
                        mode=mode,
                        gate_hard_issues=[str(i) for i in last_gate.get("hard_issues", [])],
                        gate_soft_issues=[str(i) for i in last_gate.get("soft_issues", [])],
                        recent_briefings=history_items,
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
                            "novelty": 100,
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
                feedback.extend(
                    [str(i) for i in last_verification.get("recommendations", [])]
                )
                missing_items = self._missing_items_for_markdown(
                    briefing_body, selected_items
                )
                missing_urls = [
                    str(i.get("url", "")) for i in missing_items if i.get("url")
                ]

                # If coverage keeps regressing, inject deterministic fallback links
                # before asking the model to rewrite again.
                if missing_items:
                    logger.warning(
                        "URL coverage regression detected before rewrite; appending %d missing references.",
                        len(missing_items),
                    )
                    briefing_body = self._append_missing_items_section(
                        briefing_body, missing_items
                    )
                    briefing_body = self._normalize_section_headings(
                        self._dedupe_markdown_links(briefing_body.strip())
                    )
                    briefing_body = self._de_template_fields(briefing_body)

                feedback_context = self._build_rewrite_feedback_context(
                    gate=last_gate,
                    critique=last_critique,
                    verification=last_verification,
                    mode=mode,
                    min_chars=min_chars,
                    target_chars=target_chars,
                    hard_max_chars=hard_max_chars,
                    rewrite_attempt=rewrites + 1,
                    max_rewrites=max_rewrites,
                    selected_items=selected_items,
                    missing_selected_urls=missing_urls,
                )

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
                rewritten_output = await self.writer_llm.revise_briefing(
                    draft_markdown=briefing_body,
                    items=selected_items,
                    feedback=feedback,
                    feedback_context=feedback_context,
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
                trace_finalized = True
                await enqueue_event_safe(
                    event_queue,
                    new_agent_text_message(
                        self._compose_handoff_message(
                            workflow_mode=workflow_mode,
                            decision="blocked",
                            item_count=len(selected_items),
                            human_message=msg,
                        )
                    ),
                )
                return

            briefing_body = self._normalize_section_headings(briefing_body)
            briefing_body = self._de_template_fields(briefing_body)

            logger.info("LLM briefing generated (%d chars)", len(briefing_body))

            topics = "\n".join(f"- {i['title']}: {i['summary']}" for i in selected_items)
            cover_prompt = await self.writer_llm.generate_cover_prompt(topics)
            logger.info("Cover prompt: %s", cover_prompt[:100])

            cover_url = ""
            if self.writer_llm.supports_cover_image_generation():
                try:
                    cover_url = (
                        await self.writer_llm.generate_cover_image_data_url(cover_prompt)
                        or ""
                    )
                    if cover_url:
                        logger.info("Cover image generated via Gemini image model")
                except Exception:
                    logger.exception(
                        "Failed to generate Gemini cover image, falling back to ComfyUI"
                    )
            try:
                if not cover_url:
                    timestamp = int(time.time() * 1000)
                    prefix = f"Digest_Cover_{timestamp}"
                    cover_url = await self.comfyui.generate_image(cover_prompt, prefix)
                    logger.info("Cover image: %s", cover_url)
            except Exception:
                logger.exception("Failed to generate cover image, continuing without it")

            markdown = self._format_markdown(briefing_body, cover_url, mode=mode)
            html = self._format_html(briefing_body, cover_url, mode=mode)

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
            trace_finalized = True

            msg = f"Briefing created: id={briefing_id} items={len(selected_items)}"
            logger.info(msg)
            await enqueue_event_safe(
                event_queue,
                new_agent_text_message(
                    self._compose_handoff_message(
                        workflow_mode=workflow_mode,
                        decision="publish",
                        briefing_id=briefing_id,
                        item_count=len(selected_items),
                        human_message=msg,
                    )
                ),
            )
        except Exception as exc:
            logger.exception("Writer execution failed")
            if not trace_finalized:
                await self._trace_finalize_run(
                    run_id=trace_run_id,
                    decision="BLOCKED",
                    decision_reason=f"writer_internal_error:{type(exc).__name__}",
                    rewrite_count=rewrites,
                    final_draft=briefing_body,
                    final_gate=last_gate,
                    final_critique=last_critique,
                    final_verifier=last_verification,
                    briefing_id=None,
                )
            await enqueue_event_safe(
                event_queue,
                new_agent_text_message(
                    self._compose_handoff_message(
                        workflow_mode=workflow_mode,
                        decision="blocked",
                        item_count=len(selected_items),
                        human_message=(
                            "Blocking publish: internal writer error during generation."
                        ),
                    )
                ),
            )
            return

    # -- delegate methods (unchanged signatures) --

    def _select_items_for_briefing(
        self,
        items: list[dict],
        recent_published: list[dict] | None = None,
        *,
        quiet_mode: bool = False,
    ) -> list[dict]:
        return self.service.select_items_for_briefing(
            items=items,
            recent_published=recent_published,
            quiet_mode=quiet_mode,
        )

    def _priority_score(
        self,
        item: dict,
        recent_published: list[dict] | None = None,
    ) -> float:
        return self.service.priority_score(item, recent_published)

    def _passes_source_floor(self, item: dict) -> bool:
        return self.service.passes_source_floor(item)

    def _is_quiet_day(self, items: list[dict]) -> bool:
        return self.service.is_quiet_day(items)

    @staticmethod
    def _resolve_workflow_mode(text: str) -> str:
        """Resolve workflow mode from incoming skill text."""
        for token in str(text or "").split("::"):
            candidate = token.strip().lower()
            if candidate.startswith("mode="):
                candidate = candidate.split("=", 1)[1].strip().lower()
            if candidate in _SUPPORTED_WORKFLOW_MODES:
                return candidate
        return REGULAR_DAILY_BRIEFING_MODE

    def _select_items_for_monthly_newsletter(
        self,
        items: list[dict],
    ) -> list[dict]:
        return self.service.select_items_for_monthly_newsletter(items)

    def _char_limits(
        self, mode: str, selected_count: int | None = None
    ) -> tuple[int, int, int]:
        return self.service.char_limits(mode, selected_count=selected_count)

    def _quality_gate(
        self,
        markdown: str,
        selected_items: list[dict],
        mode: str,
        min_chars: int,
        hard_max_chars: int,
    ) -> dict[str, object]:
        return self.service.quality_gate(
            markdown=markdown,
            selected_items=selected_items,
            mode=mode,
            min_chars=min_chars,
            hard_max_chars=hard_max_chars,
        )

    def _passes_critic_thresholds(self, critique: dict[str, object]) -> bool:
        return self.service.passes_critic_thresholds(critique)

    @staticmethod
    def _has_critical_critic_issue(critique: dict[str, object]) -> bool:
        return WriterService.has_critical_critic_issue(critique)

    async def _postprocess_briefing(
        self,
        briefing_body: str,
        selected_items: list[dict],
        mode: str,
        min_chars: int,
        target_chars: int,
        hard_max_chars: int,
    ) -> str:
        return await self.service.postprocess_briefing(
            briefing_body=briefing_body,
            selected_items=selected_items,
            mode=mode,
            min_chars=min_chars,
            target_chars=target_chars,
            hard_max_chars=hard_max_chars,
        )

    @staticmethod
    def _dedupe_markdown_links(markdown: str) -> str:
        return WriterService.dedupe_markdown_links(markdown)

    @staticmethod
    def _normalize_section_headings(markdown: str) -> str:
        return WriterService.normalize_section_headings(markdown)

    @staticmethod
    def _de_template_fields(markdown: str) -> str:
        return WriterService.de_template_fields(markdown)

    @staticmethod
    def _missing_items_for_markdown(markdown: str, items: list[dict]) -> list[dict]:
        return WriterService.missing_items_for_markdown(markdown, items)

    @staticmethod
    def _append_missing_items_section(markdown: str, missing_items: list[dict]) -> str:
        return WriterService.append_missing_items_section(markdown, missing_items)

    @staticmethod
    def _clip_markdown(markdown: str, limit: int) -> str:
        return WriterService.clip_markdown(markdown, limit)

    def _enforce_release_link_hygiene(
        self,
        markdown: str,
        selected_items: list[dict],
        *,
        hard_max_chars: int,
    ) -> str:
        return self.service.enforce_release_link_hygiene(
            markdown,
            selected_items,
            hard_max_chars=hard_max_chars,
        )

    @staticmethod
    def _strip_unselected_github_advisory_links(
        markdown: str, selected_items: list[dict]
    ) -> str:
        return WriterService.strip_unselected_github_advisory_links(
            markdown,
            selected_items,
        )

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
                llm_model=self.llm_client.model_for_role("writer"),
                llm_model_version=self._model_version(role="writer"),
                prompts=self.writer_llm.prompt_versions(),
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

    def _build_rewrite_feedback_context(
        self,
        *,
        gate: dict[str, object],
        critique: dict[str, object],
        verification: dict[str, object],
        mode: str,
        min_chars: int,
        target_chars: int,
        hard_max_chars: int,
        rewrite_attempt: int,
        max_rewrites: int,
        selected_items: list[dict[str, Any]],
        missing_selected_urls: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.service.build_rewrite_feedback_context(
            gate=gate,
            critique=critique,
            verification=verification,
            mode=mode,
            min_chars=min_chars,
            target_chars=target_chars,
            hard_max_chars=hard_max_chars,
            rewrite_attempt=rewrite_attempt,
            max_rewrites=max_rewrites,
            selected_items=selected_items,
            missing_selected_urls=missing_selected_urls,
        )

    @staticmethod
    def _string_list(value: object, *, limit: int = 16) -> list[str]:
        return WriterService.string_list(value, limit=limit)

    def _model_version(self, role: str) -> str:
        model = (self.llm_client.model_for_role(role) or "").strip()
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

        collector_keys = tuple(
            prefix for prefix in ("ghsa_", "rss_", "reddit_", "twitter_", "scrape_")
        )
        writer_keys = tuple(
            prefix
            for prefix in (
                "briefing_",
                "telegram_overflow_mode",
                "monthly_newsletter_",
            )
        )
        collector = {
            key: filtered[key] for key in filtered if key.startswith(collector_keys)
        }
        analyzer = {
            key: filtered[key]
            for key in filtered
            if key.startswith("scrape_")
            or key.startswith("llm_")
            or key in {"relevance_threshold"}
        }
        writer = {
            key: filtered[key]
            for key in filtered
            if key.startswith(writer_keys)
            or key
            in {
                "relevance_threshold",
                "briefing_lookback_hours",
                "llm_timeout",
                "comfyui_url",
                "comfyui_timeout",
                "comfyui_poll_interval",
            }
            or key.startswith("llm_")
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
    def _format_markdown(
        briefing_body: str,
        cover_url: str,
        *,
        mode: str = "standard",
    ) -> str:
        return WriterService.format_markdown(
            briefing_body,
            cover_url,
            mode=mode,
        )

    @staticmethod
    def _format_html(
        briefing_body: str,
        cover_url: str,
        *,
        mode: str = "standard",
    ) -> str:
        return WriterService.format_html(
            briefing_body,
            cover_url,
            mode=mode,
        )

    @staticmethod
    def _render_html_body(markdown: str) -> str:
        return WriterService.render_html_body(markdown)

    @staticmethod
    def _inline_markdown_to_html(value: str) -> str:
        return WriterService.inline_markdown_to_html(value)
