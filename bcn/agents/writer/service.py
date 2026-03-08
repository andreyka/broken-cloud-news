"""Writer domain service shared by agent execution and evaluation lanes."""

from __future__ import annotations

import html
from dataclasses import dataclass
import logging
import re
import time
from typing import Any
from typing import Protocol
from urllib.parse import urlparse

from bcn.agents.critic.llm import CriticLLM
from bcn.agents.writer.llm import WriterLLM
from bcn.briefing import BriefingFactVerifier
from bcn.briefing import BriefingQualityGate
from bcn.briefing import BriefingSelector
from bcn.briefing import text as briefing_text
from bcn.common.comfyui import ComfyUIClient
from bcn.common.config import Settings
from bcn.common.db import get_recent_published_items
from bcn.common.llm import LLMClient
from bcn.workflows.modes import REGULAR_MONTHLY_NEWSLETTER_MODE

logger = logging.getLogger(__name__)

_CRITIC_BLOCKING_TERMS = (
    "factual overreach",
    "contradiction",
    "not in selected item",
    "not in selected items",
    "ungrounded",
    "hallucinat",
    "invalid advisory",
    "invalid link",
    "misleading claim",
)


def _default_critique() -> dict[str, object]:
    """Return a permissive critic payload when the critic is disabled."""
    return {
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


def _default_verifier() -> dict[str, object]:
    """Return a permissive verifier payload when the verifier is disabled."""
    return {
        "passed": True,
        "score": 100,
        "hard_issues": [],
        "blocking_hard_issues": [],
        "soft_issues": [],
        "issues": [],
        "recommendations": [],
    }


@dataclass(frozen=True)
class PostprocessedBriefing:
    """Finalized draft body plus the selected items it actually covers."""

    markdown: str
    selected_items: list[dict[str, Any]]


class WriterWorkflowProtocol(Protocol):
    """Protocol used by evaluation code for non-publishing writer operations."""

    settings: Settings
    writer_llm: WriterLLM
    critic_llm: CriticLLM

    async def close(self) -> None:
        """Release any resources held by the writer workflow service."""

    async def select_items_for_workflow(
        self,
        item_dicts: list[dict[str, Any]],
        workflow_mode: str,
    ) -> dict[str, Any]:
        """Return the side-effect-free selection plan for one workflow mode."""

    async def evaluate_existing_markdown(
        self,
        *,
        markdown: str,
        selected_items: list[dict[str, Any]],
        history: list[dict[str, Any]],
        mode: str,
    ) -> dict[str, Any]:
        """Run release checks against an existing markdown draft."""

    async def generate_release_candidate(
        self,
        *,
        selected_items: list[dict[str, Any]],
        history: list[dict[str, Any]],
        mode: str,
    ) -> dict[str, Any]:
        """Generate a non-publishing release candidate."""

    async def simulate_briefing_body(
        self,
        items: list[dict[str, Any]],
        recent_briefings: list[dict[str, Any]],
        *,
        apply_critic_rewrites: bool,
    ) -> tuple[str, dict[str, object]]:
        """Generate a replay candidate body without inserting a briefing row."""

    async def critique_markdown(
        self,
        draft_markdown: str,
        items: list[dict[str, Any]],
        *,
        mode: str,
        recent_briefings: list[dict[str, Any]] | None = None,
        gate_hard_issues: list[str] | None = None,
        gate_soft_issues: list[str] | None = None,
    ) -> dict[str, Any]:
        """Critique markdown for release readiness."""

    def char_limits(
        self,
        mode: str,
        *,
        selected_count: int | None = None,
    ) -> tuple[int, int, int]:
        """Return min/target/max body lengths for a given mode."""

    def quality_gate(
        self,
        markdown: str,
        selected_items: list[dict[str, Any]],
        *,
        mode: str,
        min_chars: int,
        hard_max_chars: int,
    ) -> dict[str, object]:
        """Run deterministic release checks against markdown."""


class WriterService:
    """Domain service for item selection, release checks, and draft generation."""

    def __init__(
        self,
        settings: Settings,
        *,
        llm_client: LLMClient | None = None,
    ) -> None:
        self.settings = settings
        self._owns_llm_client = llm_client is None
        self.llm_client = llm_client if llm_client is not None else LLMClient.from_settings(settings)
        self.writer_llm = WriterLLM(self.llm_client)
        self.critic_llm = CriticLLM(self.llm_client)
        self.selector = BriefingSelector(settings)
        self.quality = BriefingQualityGate(settings)
        self.verifier = BriefingFactVerifier(settings, llm_client=self.llm_client)
        self.comfyui = ComfyUIClient(
            base_url=settings.comfyui_url,
            timeout=settings.comfyui_timeout,
            poll_interval=settings.comfyui_poll_interval,
        )

    async def close(self) -> None:
        """Release resources owned by this writer service."""
        await self.writer_llm.close()
        await self.verifier.close()
        await self.comfyui.close()
        if self._owns_llm_client:
            await self.llm_client.close()

    async def select_items_for_workflow(
        self,
        item_dicts: list[dict[str, Any]],
        workflow_mode: str,
    ) -> dict[str, Any]:
        """Select items for one workflow mode without mutating DB state."""
        if workflow_mode == REGULAR_MONTHLY_NEWSLETTER_MODE:
            selected = self.select_items_for_monthly_newsletter(item_dicts)
            if not selected:
                return {
                    "decision": "skip",
                    "reason": "not_enough_diverse_items_after_monthly_selection",
                    "message": (
                        "Monthly newsletter skipped: not enough diverse high-signal "
                        "items after selection constraints."
                    ),
                    "mode": "monthly_newsletter",
                    "selected_items": [],
                }
            return {
                "decision": "generate",
                "reason": "monthly_selection_ready",
                "message": "",
                "mode": "monthly_newsletter",
                "selected_items": selected,
            }

        if bool(self.settings.briefing_skip_if_no_high_signal):
            high_signal = self.selector.high_signal_count(item_dicts)
            min_high_signal = max(
                1, int(self.settings.briefing_min_high_signal_to_publish)
            )
            if high_signal < min_high_signal:
                return {
                    "decision": "skip",
                    "reason": (
                        f"high_signal_below_threshold:{high_signal}<{min_high_signal}"
                    ),
                    "message": (
                        "Quiet day — not enough high-signal items "
                        f"({high_signal} < {min_high_signal}). Skipping briefing."
                    ),
                    "mode": "standard",
                    "selected_items": [],
                }

        recent_published = await get_recent_published_items(
            hours=self.settings.briefing_novelty_lookback_hours,
            limit=self.settings.briefing_novelty_max_items,
        )
        quiet_mode = self.is_quiet_day(item_dicts)
        mode = "quiet_day" if quiet_mode else "standard"
        selected = self.select_items_for_briefing(
            item_dicts,
            recent_published=[dict(row) for row in recent_published],
            quiet_mode=quiet_mode,
        )
        if not selected:
            return {
                "decision": "skip",
                "reason": "no_items_remained_after_selection_constraints",
                "message": "No items remained after selection constraints. Skipping briefing.",
                "mode": mode,
                "selected_items": [],
            }
        return {
            "decision": "generate",
            "reason": "selection_ready",
            "message": "",
            "mode": mode,
            "selected_items": selected,
        }

    async def critique_markdown(
        self,
        draft_markdown: str,
        items: list[dict[str, Any]],
        *,
        mode: str,
        recent_briefings: list[dict[str, Any]] | None = None,
        gate_hard_issues: list[str] | None = None,
        gate_soft_issues: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run the critic or return a permissive default payload."""
        if not self.settings.briefing_critique_enabled:
            return _default_critique()
        return await self.critic_llm.critique_briefing(
            draft_markdown=draft_markdown,
            items=items,
            mode=mode,
            gate_hard_issues=gate_hard_issues or [],
            gate_soft_issues=gate_soft_issues or [],
            recent_briefings=recent_briefings or [],
        )

    async def verify_markdown(
        self,
        markdown: str,
        selected_items: list[dict[str, Any]],
        *,
        mode: str,
    ) -> dict[str, Any]:
        """Run factual verification or return a permissive default payload."""
        if not self.settings.briefing_verifier_enabled:
            return _default_verifier()
        return await self.verifier.evaluate(markdown, selected_items, mode=mode)

    async def evaluate_existing_markdown(
        self,
        *,
        markdown: str,
        selected_items: list[dict[str, Any]],
        history: list[dict[str, Any]],
        mode: str,
    ) -> dict[str, Any]:
        """Score one existing markdown draft against current release checks."""
        min_chars, target_chars, hard_max_chars = self.char_limits(
            mode,
            selected_count=len(selected_items),
        )
        normalized = self.normalize_section_headings(
            self.dedupe_markdown_links((markdown or "").strip())
        )
        normalized = self.de_template_fields(normalized)
        normalized = self.enforce_release_link_hygiene(
            normalized,
            selected_items,
            hard_max_chars=hard_max_chars,
        )

        gate = self.quality_gate(
            markdown=normalized,
            selected_items=selected_items,
            mode=mode,
            min_chars=min_chars,
            hard_max_chars=hard_max_chars,
        )
        critique = await self.critique_markdown(
            normalized,
            selected_items,
            mode=mode,
            gate_hard_issues=[str(issue) for issue in gate.get("hard_issues", [])],
            gate_soft_issues=[str(issue) for issue in gate.get("soft_issues", [])],
            recent_briefings=history,
        )
        verifier = await self.verify_markdown(
            normalized,
            selected_items,
            mode=mode,
        )
        critique_passed = self.passes_critic_thresholds(critique)
        release_passed = (
            bool(gate.get("passed", False))
            and critique_passed
            and bool(verifier.get("passed", True))
        )
        return {
            "markdown": normalized,
            "mode": mode,
            "min_chars": min_chars,
            "target_chars": target_chars,
            "hard_max_chars": hard_max_chars,
            "gate": gate,
            "critique": critique,
            "verifier": verifier,
            "release_passed": release_passed,
        }

    async def generate_release_candidate(
        self,
        *,
        selected_items: list[dict[str, Any]],
        history: list[dict[str, Any]],
        mode: str,
    ) -> dict[str, Any]:
        """Generate and evaluate one release candidate without publishing it."""
        min_chars, target_chars, hard_max_chars = self.char_limits(
            mode,
            selected_count=len(selected_items),
        )
        draft = await self.writer_llm.generate_briefing(
            selected_items,
            recent_briefings=history,
            mode=mode,
        )
        active_selected_items = list(selected_items)
        postprocessed = await self.postprocess_briefing(
            briefing_body=draft,
            selected_items=active_selected_items,
            mode=mode,
            min_chars=min_chars,
            target_chars=target_chars,
            hard_max_chars=hard_max_chars,
        )
        draft = postprocessed.markdown
        active_selected_items = postprocessed.selected_items

        rewrites = 0
        max_rewrites = max(0, int(self.settings.briefing_critique_max_rounds))
        trace_rounds: list[dict[str, Any]] = []
        preference_pairs: list[dict[str, Any]] = []
        while True:
            evaluation = await self.evaluate_existing_markdown(
                markdown=draft,
                selected_items=active_selected_items,
                history=history,
                mode=mode,
            )
            round_input = str(evaluation["markdown"] or "")
            evaluation["rewrites"] = rewrites
            evaluation["selected_items"] = list(active_selected_items)
            if bool(evaluation["release_passed"]):
                trace_rounds.append(
                    {
                        "round_index": len(trace_rounds),
                        "phase": "initial" if not trace_rounds else "rewrite",
                        "draft_input": round_input,
                        "gate_result": dict(evaluation["gate"]),
                        "critique_result": dict(evaluation["critique"]),
                        "verifier_result": dict(evaluation["verifier"]),
                        "feedback": [],
                        "rewrite_output": None,
                        "passed": True,
                    }
                )
                evaluation["rounds"] = trace_rounds
                evaluation["preference_pairs"] = preference_pairs
                return evaluation
            if rewrites >= max_rewrites:
                trace_rounds.append(
                    {
                        "round_index": len(trace_rounds),
                        "phase": "initial" if not trace_rounds else "rewrite",
                        "draft_input": round_input,
                        "gate_result": dict(evaluation["gate"]),
                        "critique_result": dict(evaluation["critique"]),
                        "verifier_result": dict(evaluation["verifier"]),
                        "feedback": [],
                        "rewrite_output": None,
                        "passed": False,
                    }
                )
                evaluation["rounds"] = trace_rounds
                evaluation["preference_pairs"] = preference_pairs
                return evaluation

            gate = evaluation["gate"]
            critique = evaluation["critique"]
            verifier = evaluation["verifier"]
            feedback: list[str] = []
            feedback.extend(str(issue) for issue in gate.get("issues", []))
            feedback.extend(str(issue) for issue in critique.get("issues", []))
            feedback.extend(str(issue) for issue in critique.get("recommendations", []))
            feedback.extend(str(issue) for issue in verifier.get("issues", []))
            feedback.extend(
                str(issue) for issue in verifier.get("recommendations", [])
            )
            missing_items = self.missing_items_for_markdown(draft, active_selected_items)
            missing_urls = [
                str(item.get("url", "")) for item in missing_items if item.get("url")
            ]
            if missing_items:
                draft = self.append_missing_items_section(draft, missing_items)
                draft = self.normalize_section_headings(
                    self.dedupe_markdown_links(draft.strip())
                )
                draft = self.de_template_fields(draft)

            feedback_context = self.build_rewrite_feedback_context(
                gate=gate,
                critique=critique,
                verification=verifier,
                mode=mode,
                min_chars=int(evaluation["min_chars"]),
                target_chars=int(evaluation["target_chars"]),
                hard_max_chars=int(evaluation["hard_max_chars"]),
                rewrite_attempt=rewrites + 1,
                max_rewrites=max_rewrites,
                selected_items=active_selected_items,
                missing_selected_urls=missing_urls,
            )

            rewrites += 1
            rewritten_output = await self.writer_llm.revise_briefing(
                draft_markdown=draft,
                items=active_selected_items,
                feedback=feedback,
                feedback_context=feedback_context,
                recent_briefings=history,
                mode=mode,
                min_chars=int(evaluation["min_chars"]),
                target_chars=int(evaluation["target_chars"]),
                hard_max_chars=int(evaluation["hard_max_chars"]),
            )
            postprocessed = await self.postprocess_briefing(
                briefing_body=rewritten_output,
                selected_items=active_selected_items,
                mode=mode,
                min_chars=int(evaluation["min_chars"]),
                target_chars=int(evaluation["target_chars"]),
                hard_max_chars=int(evaluation["hard_max_chars"]),
            )
            rewritten_output = postprocessed.markdown
            active_selected_items = postprocessed.selected_items
            trace_rounds.append(
                {
                    "round_index": len(trace_rounds),
                    "phase": "initial" if not trace_rounds else "rewrite",
                    "draft_input": round_input,
                    "gate_result": dict(gate),
                    "critique_result": dict(critique),
                    "verifier_result": dict(verifier),
                    "feedback": [str(item) for item in feedback],
                    "rewrite_output": rewritten_output,
                    "passed": False,
                }
            )
            preference_pairs.append(
                {
                    "round_index": len(trace_rounds),
                    "chosen_text": rewritten_output,
                    "rejected_text": round_input,
                    "rationale": self.build_preference_rationale(feedback),
                    "source": "auto_writer_loop",
                }
            )
            draft = rewritten_output

    async def build_release_artifact(
        self,
        *,
        briefing_body: str,
        selected_items: list[dict[str, Any]],
        mode: str,
    ) -> dict[str, str]:
        """Build final markdown/html assets and cover metadata."""
        topics = "\n".join(
            f"- {item['title']}: {item['summary']}" for item in selected_items
        )
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
        if not cover_url:
            try:
                prefix = f"Digest_Cover_{int(time.time() * 1000)}"
                cover_url = await self.comfyui.generate_image(cover_prompt, prefix)
                logger.info("Cover image: %s", cover_url)
            except Exception:
                logger.exception(
                    "Failed to generate cover image, continuing without it"
                )

        return {
            "cover_prompt": cover_prompt,
            "cover_url": cover_url,
            "markdown": self.format_markdown(briefing_body, cover_url, mode=mode),
            "html": self.format_html(briefing_body, cover_url, mode=mode),
        }

    async def simulate_briefing_body(
        self,
        items: list[dict[str, Any]],
        recent_briefings: list[dict[str, Any]],
        *,
        apply_critic_rewrites: bool,
    ) -> tuple[str, dict[str, object]]:
        """Generate a replay candidate body without storing a draft row."""
        quiet_mode = self.is_quiet_day(items)
        mode = "quiet_day" if quiet_mode else "standard"
        min_chars, target_chars, hard_max_chars = self.char_limits(
            mode,
            selected_count=len(items),
        )
        active_items = list(items)

        briefing_body = await self.writer_llm.generate_briefing(
            active_items,
            recent_briefings=recent_briefings,
            mode=mode,
        )
        postprocessed = await self.postprocess_briefing(
            briefing_body=briefing_body,
            selected_items=active_items,
            mode=mode,
            min_chars=min_chars,
            target_chars=target_chars,
            hard_max_chars=hard_max_chars,
        )
        briefing_body = postprocessed.markdown
        active_items = postprocessed.selected_items
        min_chars, target_chars, hard_max_chars = self.char_limits(
            mode,
            selected_count=len(active_items),
        )

        rewrites = 0
        if apply_critic_rewrites and self.settings.briefing_critique_enabled:
            max_rewrites = max(0, int(self.settings.briefing_critique_max_rounds))
            while True:
                min_chars, target_chars, hard_max_chars = self.char_limits(
                    mode,
                    selected_count=len(active_items),
                )
                gate = self.quality_gate(
                    markdown=briefing_body,
                    selected_items=active_items,
                    mode=mode,
                    min_chars=min_chars,
                    hard_max_chars=hard_max_chars,
                )
                critique = await self.critique_markdown(
                    briefing_body,
                    active_items,
                    mode=mode,
                    gate_hard_issues=[
                        str(issue) for issue in gate.get("hard_issues", [])
                    ],
                    gate_soft_issues=[
                        str(issue) for issue in gate.get("soft_issues", [])
                    ],
                    recent_briefings=recent_briefings,
                )
                gate_passed = bool(gate.get("passed", False))
                critic_passed = bool(critique.get("passed", False))
                if gate_passed and critic_passed:
                    break
                if rewrites >= max_rewrites:
                    break

                feedback: list[str] = []
                feedback.extend(gate.get("issues", []))
                feedback.extend([str(issue) for issue in critique.get("issues", [])])
                feedback.extend(
                    [str(issue) for issue in critique.get("recommendations", [])]
                )
                missing_items = self.missing_items_for_markdown(
                    briefing_body,
                    active_items,
                )
                feedback_context = self.build_rewrite_feedback_context(
                    gate=gate,
                    critique=critique,
                    verification=_default_verifier(),
                    mode=mode,
                    min_chars=min_chars,
                    target_chars=target_chars,
                    hard_max_chars=hard_max_chars,
                    rewrite_attempt=rewrites + 1,
                    max_rewrites=max_rewrites,
                    selected_items=active_items,
                    missing_selected_urls=[
                        str(item.get("url", "")) for item in missing_items if item.get("url")
                    ],
                )

                rewrites += 1
                briefing_body = await self.writer_llm.revise_briefing(
                    draft_markdown=briefing_body,
                    items=active_items,
                    feedback=feedback,
                    feedback_context=feedback_context,
                    mode=mode,
                    min_chars=min_chars,
                    target_chars=target_chars,
                    hard_max_chars=hard_max_chars,
                )
                postprocessed = await self.postprocess_briefing(
                    briefing_body=briefing_body,
                    selected_items=active_items,
                    mode=mode,
                    min_chars=min_chars,
                    target_chars=target_chars,
                    hard_max_chars=hard_max_chars,
                )
                briefing_body = postprocessed.markdown
                active_items = postprocessed.selected_items

        briefing_body = self.normalize_section_headings(briefing_body)
        briefing_body = self.de_template_fields(briefing_body)
        min_chars, target_chars, hard_max_chars = self.char_limits(
            mode,
            selected_count=len(active_items),
        )

        meta = {
            "mode": mode,
            "rewrites": rewrites,
            "min_chars": min_chars,
            "hard_max_chars": hard_max_chars,
            "selected_items": list(active_items),
        }
        return briefing_body, meta

    def select_items_for_briefing(
        self,
        items: list[dict[str, Any]],
        recent_published: list[dict[str, Any]] | None = None,
        *,
        quiet_mode: bool = False,
    ) -> list[dict[str, Any]]:
        """Select daily briefing items with novelty and diversity constraints."""
        return self.selector.select_items(
            items=items,
            recent_published=recent_published,
            quiet_mode=quiet_mode,
        )

    def priority_score(
        self,
        item: dict[str, Any],
        recent_published: list[dict[str, Any]] | None = None,
    ) -> float:
        """Return the selector priority score for one item."""
        return self.selector.priority_score(item, recent_published)

    def passes_source_floor(self, item: dict[str, Any]) -> bool:
        """Return whether an item clears the selector's source floor."""
        return self.selector.passes_source_floor(item)

    def is_quiet_day(self, items: list[dict[str, Any]]) -> bool:
        """Return whether the pool should use quiet-day briefing mode."""
        return self.selector.is_quiet_day(items)

    def select_items_for_monthly_newsletter(
        self,
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Select a broader, story-deduped set of items for monthly mode."""
        min_items = max(1, int(self.settings.monthly_newsletter_min_items))
        max_items = max(min_items, int(self.settings.monthly_newsletter_max_items))
        per_domain_cap = max(
            1, int(self.settings.monthly_newsletter_max_items_per_domain)
        )

        ranked = sorted(
            items,
            key=lambda item: (
                int(item.get("relevance_score", 0) or 0),
                self.priority_score(item),
            ),
            reverse=True,
        )
        selected: list[dict[str, Any]] = []
        domain_counts: dict[str, int] = {}
        for item in ranked:
            if self.selector.is_duplicate_of(item, selected):
                continue
            url = str(item.get("url", "") or "")
            domain = (urlparse(url).netloc or "").strip().lower()
            if domain and domain_counts.get(domain, 0) >= per_domain_cap:
                continue
            selected.append(item)
            if domain:
                domain_counts[domain] = domain_counts.get(domain, 0) + 1
            if len(selected) >= max_items:
                break
        return selected if len(selected) >= min_items else []

    def char_limits(
        self,
        mode: str,
        *,
        selected_count: int | None = None,
    ) -> tuple[int, int, int]:
        """Return min/target/max body lengths for a given mode."""
        min_chars, target_chars, hard_max_chars = self.quality.char_limits(mode)
        if selected_count is not None and selected_count <= 1:
            min_chars = min(
                min_chars,
                int(self.settings.briefing_single_item_min_chars),
            )
            target_chars = min(
                target_chars,
                int(self.settings.briefing_single_item_target_chars),
            )
            hard_max_chars = min(
                hard_max_chars,
                int(self.settings.briefing_single_item_hard_max_chars),
            )
            target_chars = max(min_chars, target_chars)
            hard_max_chars = max(target_chars, hard_max_chars)
        return min_chars, target_chars, hard_max_chars

    def quality_gate(
        self,
        markdown: str,
        selected_items: list[dict[str, Any]],
        *,
        mode: str,
        min_chars: int,
        hard_max_chars: int,
    ) -> dict[str, object]:
        """Run deterministic release checks against markdown."""
        return self.quality.evaluate(
            markdown=markdown,
            selected_items=selected_items,
            mode=mode,
            min_chars=min_chars,
            hard_max_chars=hard_max_chars,
        )

    def passes_critic_thresholds(self, critique: dict[str, object]) -> bool:
        """Apply blocking thresholds for critic score and key dimensions."""
        if not critique:
            return False
        if not bool(critique.get("passed", False)):
            return False
        if self.has_critical_critic_issue(critique):
            return False

        score = int(critique.get("score", 0) or 0)
        dims = critique.get("dimension_scores", {}) or {}
        if not isinstance(dims, dict):
            dims = {}
        actionability = int(dims.get("actionability", 0) or 0)
        link_hygiene = int(dims.get("link_hygiene", 0) or 0)

        return (
            score >= int(self.settings.briefing_critic_min_score)
            and actionability >= int(self.settings.briefing_critic_min_actionability)
            and link_hygiene >= int(self.settings.briefing_critic_min_link_hygiene)
        )

    @staticmethod
    def has_critical_critic_issue(critique: dict[str, object]) -> bool:
        """Return whether the critic payload contains a blocking issue."""
        issues = critique.get("issues", [])
        recommendations = critique.get("recommendations", [])
        payload: list[str] = []
        if isinstance(issues, list):
            payload.extend(str(item) for item in issues)
        elif issues:
            payload.append(str(issues))
        if isinstance(recommendations, list):
            payload.extend(str(item) for item in recommendations)
        elif recommendations:
            payload.append(str(recommendations))
        joined = " | ".join(text.lower() for text in payload if text)
        return any(term in joined for term in _CRITIC_BLOCKING_TERMS)

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
            selected_count=len(active_selected_items),
        )
        markdown = self.normalize_section_headings(
            self.dedupe_markdown_links((briefing_body or "").strip())
        )
        markdown = self.de_template_fields(markdown)

        for _ in range(2):
            missing_items = self.missing_items_for_markdown(
                markdown,
                active_selected_items,
            )
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
            markdown = self.normalize_section_headings(
                self.dedupe_markdown_links(markdown.strip())
            )
            markdown = self.de_template_fields(markdown)

        missing_items = self.missing_items_for_markdown(markdown, active_selected_items)
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
            weakest = min(
                missing_items,
                key=lambda item: self.priority_score(item),
            )
            active_selected_items = [
                item
                for item in active_selected_items
                if str(item.get("id")) != str(weakest.get("id"))
            ]
            current_min_chars, current_target_chars, current_hard_max_chars = self.char_limits(
                mode,
                selected_count=len(active_selected_items),
            )
            drops += 1
            logger.warning(
                "Dropping uncovered low-priority item after rewrite retries: %s (%s)",
                weakest.get("title"),
                weakest.get("url"),
            )

            missing_items = self.missing_items_for_markdown(
                markdown,
                active_selected_items,
            )
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
            markdown = self.normalize_section_headings(
                self.dedupe_markdown_links(markdown.strip())
            )
            markdown = self.de_template_fields(markdown)
            missing_items = self.missing_items_for_markdown(
                markdown,
                active_selected_items,
            )

        if missing_items:
            logger.warning(
                "Coverage fallback appending %d missing selected item references.",
                len(missing_items),
            )
            markdown = self.append_missing_items_section(markdown, missing_items)
            markdown = self.normalize_section_headings(
                self.dedupe_markdown_links(markdown.strip())
            )
            markdown = self.de_template_fields(markdown)

        if len(markdown) > current_hard_max_chars:
            markdown = await self.writer_llm.tighten_briefing(
                markdown=markdown,
                target_chars=current_target_chars,
                hard_max_chars=current_hard_max_chars,
            )
            markdown = self.normalize_section_headings(
                self.dedupe_markdown_links(markdown.strip())
            )
            markdown = self.de_template_fields(markdown)

        if len(markdown) > current_hard_max_chars:
            markdown = self.clip_markdown(markdown, current_hard_max_chars)

        markdown = self.enforce_release_link_hygiene(
            markdown,
            active_selected_items,
            hard_max_chars=current_hard_max_chars,
        )
        return PostprocessedBriefing(
            markdown=markdown.strip(),
            selected_items=active_selected_items,
        )

    @staticmethod
    def dedupe_markdown_links(markdown: str) -> str:
        """Remove duplicate markdown links while preserving surrounding text."""
        return briefing_text.dedupe_markdown_links(markdown)

    @staticmethod
    def normalize_section_headings(markdown: str) -> str:
        """Normalize digest section headings to the house style."""
        return briefing_text.normalize_section_headings(markdown)

    @staticmethod
    def de_template_fields(markdown: str) -> str:
        """Strip template filler fields left by the model."""
        return briefing_text.de_template_fields(markdown)

    @staticmethod
    def missing_items_for_markdown(markdown: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return selected items whose URLs are missing from markdown."""
        return briefing_text.missing_items_for_markdown(markdown, items)

    @staticmethod
    def append_missing_items_section(
        markdown: str,
        missing_items: list[dict[str, Any]],
    ) -> str:
        """Append a deterministic references section for missing URLs."""
        return briefing_text.append_missing_items_section(markdown, missing_items)

    @staticmethod
    def clip_markdown(markdown: str, limit: int) -> str:
        """Clip markdown to a hard size limit while preserving readability."""
        return briefing_text.clip_markdown(markdown, limit)

    def enforce_release_link_hygiene(
        self,
        markdown: str,
        selected_items: list[dict[str, Any]],
        *,
        hard_max_chars: int,
    ) -> str:
        """Apply deterministic URL cleanup just before release checks."""
        cleaned = self.strip_unselected_markdown_links(markdown, selected_items)
        cleaned = self.normalize_section_headings(
            self.dedupe_markdown_links((cleaned or "").strip())
        )
        cleaned = self.de_template_fields(cleaned)

        missing_items = self.missing_items_for_markdown(cleaned, selected_items)
        if missing_items:
            logger.warning(
                "Final deterministic coverage pass appending %d missing selected item references.",
                len(missing_items),
            )
            cleaned = self.append_missing_items_section(cleaned, missing_items)
            cleaned = self.normalize_section_headings(
                self.dedupe_markdown_links(cleaned.strip())
            )
            cleaned = self.de_template_fields(cleaned)

        if len(cleaned) > hard_max_chars:
            cleaned = self.clip_markdown(cleaned, hard_max_chars)

        return cleaned.strip()

    @staticmethod
    def strip_unselected_markdown_links(
        markdown: str,
        selected_items: list[dict[str, Any]],
    ) -> str:
        """Drop markdown-link formatting for any URL outside the selected item set."""
        selected_keys = {
            briefing_text.canonical_url_key(str(item.get("url", "")))
            for item in selected_items
            if item.get("url")
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

    def build_rewrite_feedback_context(
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
        """Build compact structured rewrite guidance for the LLM."""
        gate_hard = self.string_list(gate.get("hard_issues"), limit=16)
        gate_soft = self.string_list(gate.get("soft_issues"), limit=16)
        gate_issues = self.string_list(gate.get("issues"), limit=20)
        critic_issues = self.string_list(critique.get("issues"), limit=16)
        critic_recommendations = self.string_list(
            critique.get("recommendations"),
            limit=16,
        )
        verifier_hard = self.string_list(verification.get("hard_issues"), limit=16)
        verifier_blocking_hard = self.string_list(
            verification.get("blocking_hard_issues"),
            limit=16,
        )
        verifier_soft = self.string_list(verification.get("soft_issues"), limit=16)
        verifier_recommendations = self.string_list(
            verification.get("recommendations"),
            limit=16,
        )

        critic_dims = critique.get("dimension_scores", {})
        if not isinstance(critic_dims, dict):
            critic_dims = {}
        min_thresholds = {
            "score": int(self.settings.briefing_critic_min_score),
            "actionability": int(self.settings.briefing_critic_min_actionability),
            "link_hygiene": int(self.settings.briefing_critic_min_link_hygiene),
        }
        failed_critic_thresholds: list[str] = []
        critic_score = int(critique.get("score", 0) or 0)
        if critic_score < min_thresholds["score"]:
            failed_critic_thresholds.append(
                f"score {critic_score} < {min_thresholds['score']}"
            )
        for dimension in ("actionability", "link_hygiene"):
            dim_score = int(critic_dims.get(dimension, 0) or 0)
            if dim_score < min_thresholds[dimension]:
                failed_critic_thresholds.append(
                    f"{dimension} {dim_score} < {min_thresholds[dimension]}"
                )

        compact_items: list[dict[str, Any]] = []
        for item in selected_items[:12]:
            compact_items.append(
                {
                    "url": str(item.get("url", "")).strip(),
                    "title": str(item.get("title", "")).strip(),
                    "source_type": str(item.get("source_type", "")).strip(),
                    "relevance_score": int(item.get("relevance_score", 0) or 0),
                }
            )

        priorities: list[str] = []
        if gate_hard:
            priorities.append("Resolve gate hard issues first (blocking release).")
        if verifier_blocking_hard:
            priorities.append(
                "Resolve verifier deterministic blocking issues before style changes."
            )
        if verifier_hard:
            priorities.append(
                "Address verifier hard issues to tighten factual grounding."
            )
        if failed_critic_thresholds:
            priorities.append(
                "Raise critic threshold failures: "
                + "; ".join(failed_critic_thresholds[:3])
            )
        if not priorities:
            priorities.append(
                "Improve clarity and actionability while preserving exact URL coverage."
            )

        return {
            "rewrite": {
                "attempt": int(max(1, rewrite_attempt)),
                "max_attempts": int(max(0, max_rewrites)),
                "mode": mode,
                "target_length_chars": {
                    "min": int(min_chars),
                    "target": int(target_chars),
                    "hard_max": int(hard_max_chars),
                },
            },
            "release_status": {
                "gate_passed": bool(gate.get("passed", False)),
                "critic_passed": self.passes_critic_thresholds(critique),
                "verifier_passed": bool(verification.get("passed", True)),
            },
            "priority_order": priorities[:6],
            "blocking": {
                "gate_hard_issues": gate_hard,
                "verifier_blocking_hard_issues": verifier_blocking_hard,
                "verifier_hard_issues": verifier_hard,
            },
            "gate": {
                "issues": gate_issues,
                "soft_issues": gate_soft,
            },
            "critic": {
                "passed": bool(critique.get("passed", False)),
                "score": critic_score,
                "dimension_scores": {
                    "actionability": int(critic_dims.get("actionability", 0) or 0),
                    "source_diversity": int(
                        critic_dims.get("source_diversity", 0) or 0
                    ),
                    "link_hygiene": int(critic_dims.get("link_hygiene", 0) or 0),
                    "clarity": int(critic_dims.get("clarity", 0) or 0),
                    "style": int(critic_dims.get("style", 0) or 0),
                },
                "thresholds": min_thresholds,
                "failed_thresholds": failed_critic_thresholds,
                "issues": critic_issues,
                "recommendations": critic_recommendations,
            },
            "verifier": {
                "passed": bool(verification.get("passed", True)),
                "score": int(verification.get("score", 0) or 0),
                "soft_issues": verifier_soft,
                "recommendations": verifier_recommendations,
            },
            "coverage": {
                "missing_selected_urls": [
                    str(url).strip()
                    for url in (missing_selected_urls or [])
                    if str(url).strip()
                ][:16],
                "selected_items": compact_items,
            },
        }

    @staticmethod
    def build_preference_rationale(feedback: list[str] | None) -> str:
        """Summarize rewrite feedback for preference-pair training rows."""
        normalized = [
            str(item).strip().rstrip(".")
            for item in (feedback or [])
            if str(item).strip()
        ]
        if not normalized:
            return "Rewrite preferred based on aggregate release feedback"

        summary = "; ".join(normalized[:3])
        if len(normalized) > 3:
            summary += "; additional release feedback"
        return summary[:400]

    @staticmethod
    def string_list(value: object, *, limit: int = 16) -> list[str]:
        """Normalize arbitrary payload values into a short string list."""
        if isinstance(value, list):
            raw = value
        elif value is None:
            raw = []
        else:
            raw = [value]
        out: list[str] = []
        for item in raw:
            text = str(item).strip()
            if text:
                out.append(text)
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def format_markdown(
        briefing_body: str,
        cover_url: str,
        *,
        mode: str = "standard",
    ) -> str:
        """Wrap the briefing body with an optional cover image in markdown."""
        markdown = ""
        if cover_url and cover_url.startswith(("http://", "https://")):
            alt = (
                "Monthly Newsletter Cover"
                if mode == "monthly_newsletter"
                else "Daily Cover"
            )
            markdown += f"![{alt}]({cover_url})\n\n"
        markdown += briefing_body
        return markdown

    @staticmethod
    def format_html(
        briefing_body: str,
        cover_url: str,
        *,
        mode: str = "standard",
    ) -> str:
        """Convert briefing markdown-ish text to styled HTML email markup."""
        if mode == "monthly_newsletter":
            title = "Broken Cloud News Monthly Newsletter"
            subtitle = (
                "Most interesting cloud security developments from the last month."
            )
        else:
            title = "Broken Cloud News Briefing"
            subtitle = "Cloud security highlights, analysis, and operator guidance."

        body_html = WriterService.render_html_body(briefing_body)
        cover_block = ""
        if cover_url and cover_url.startswith(("http://", "https://", "data:image/")):
            safe_cover = html.escape(cover_url, quote=True)
            cover_block = (
                "<div style=\"margin:0 0 20px 0;\">"
                f"<img src=\"{safe_cover}\" alt=\"Briefing cover\" "
                "style=\"display:block;width:100%;max-width:760px;border-radius:14px;border:1px solid #d7e3ef;\"/>"
                "</div>"
            )

        return (
            "<html><body style=\"margin:0;padding:24px;background:#f4f7fb;"
            "font-family:'Segoe UI',Arial,sans-serif;color:#142033;\">"
            "<div style=\"max-width:820px;margin:0 auto;background:#ffffff;border:1px solid #d8e3ee;"
            "border-radius:16px;overflow:hidden;box-shadow:0 10px 26px rgba(16,40,69,0.08);\">"
            "<div style=\"padding:20px 24px;background:linear-gradient(120deg,#0f243f,#1a4f7a);color:#eaf3fb;\">"
            f"<h1 style=\"margin:0 0 8px 0;font-size:28px;line-height:1.2;\">{html.escape(title)}</h1>"
            f"<p style=\"margin:0;font-size:14px;opacity:0.93;\">{html.escape(subtitle)}</p>"
            "</div>"
            "<div style=\"padding:22px 24px 26px 24px;\">"
            f"{cover_block}"
            f"{body_html}"
            "</div>"
            "</div></body></html>"
        )

    @staticmethod
    def render_html_body(markdown: str) -> str:
        """Render markdown-ish digest text into readable HTML blocks."""
        parts: list[str] = []
        in_list = False
        for raw in str(markdown or "").splitlines():
            line = raw.strip()
            if not line:
                if in_list:
                    parts.append("</ul>")
                    in_list = False
                continue

            heading_text = ""
            heading_tag = ""
            if line.startswith("## "):
                heading_text = line[3:].strip()
                heading_tag = "h2"
            elif line.startswith("### "):
                heading_text = line[4:].strip()
                heading_tag = "h3"
            else:
                bold_heading = re.fullmatch(r"\*\*(.+?)\*\*", line)
                if bold_heading:
                    heading_text = bold_heading.group(1).strip()
                    heading_tag = "h3"

            if heading_tag:
                if in_list:
                    parts.append("</ul>")
                    in_list = False
                parts.append(
                    f"<{heading_tag} style=\"margin:18px 0 10px 0;color:#143154;\">"
                    f"{WriterService.inline_markdown_to_html(heading_text)}"
                    f"</{heading_tag}>"
                )
                continue

            if line.startswith("- ") or line.startswith("* "):
                if not in_list:
                    parts.append("<ul style=\"margin:8px 0 14px 18px;padding:0;\">")
                    in_list = True
                parts.append(
                    "<li style=\"margin:0 0 8px 0;line-height:1.5;\">"
                    f"{WriterService.inline_markdown_to_html(line[2:].strip())}"
                    "</li>"
                )
                continue

            if in_list:
                parts.append("</ul>")
                in_list = False
            parts.append(
                "<p style=\"margin:0 0 12px 0;line-height:1.6;color:#1a2940;\">"
                f"{WriterService.inline_markdown_to_html(line)}"
                "</p>"
            )

        if in_list:
            parts.append("</ul>")
        return "\n".join(parts)

    @staticmethod
    def inline_markdown_to_html(value: str) -> str:
        """Convert basic inline markdown syntax to safe HTML."""
        text = html.escape(value or "", quote=True)
        text = re.sub(
            r"\[([^\]]+)\]\((https?://[^)]+)\)",
            r'<a href="\2" style="color:#1c5f96;text-decoration:underline;">\1</a>',
            text,
        )
        text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
        text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
        return text


__all__ = [
    "WriterService",
    "WriterWorkflowProtocol",
]
