"""Writer domain service used by generation workflows and evaluation lanes."""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlparse

from bcn.services.writer.llm import WriterLLM
from bcn.services.writer.models import PostprocessedBriefing
from bcn.services.writer.orchestration import (
    generate_release_candidate as run_generate_release_candidate,
)
from bcn.services.writer.orchestration import (
    simulate_briefing_body as run_simulate_briefing_body,
)
from bcn.services.writer.postprocess import WriterPostprocessor
from bcn.services.writer.postprocess import append_missing_items_section
from bcn.services.writer.postprocess import clip_markdown
from bcn.services.writer.postprocess import de_template_fields
from bcn.services.writer.postprocess import dedupe_markdown_links
from bcn.services.writer.postprocess import missing_items_for_markdown
from bcn.services.writer.postprocess import normalize_section_headings
from bcn.services.writer.rendering import format_html
from bcn.services.writer.rendering import format_markdown
from bcn.services.writer.rendering import inline_markdown_to_html
from bcn.services.writer.rendering import render_html_body
from bcn.briefing import BriefingQualityGate
from bcn.briefing import BriefingSelector
from bcn.common.comfyui import ComfyUIClient
from bcn.common.config import Settings
from bcn.common.llm import LLMClient
from bcn.contracts.review import CritiqueRequest
from bcn.contracts.review import VerificationRequest
from bcn.contracts.services import CriticEvaluator
from bcn.contracts.services import VerificationEvaluator
from bcn.contracts.services import WriterTraceMetadata
from bcn.contracts.services import WriterWorkflow
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


class WriterService:
    """Domain service for item selection, release checks, and draft generation."""

    def __init__(
        self,
        settings: Settings,
        *,
        llm_client: LLMClient | None = None,
        critic_evaluator: CriticEvaluator | None = None,
        verifier_evaluator: VerificationEvaluator | None = None,
        owns_critic_evaluator: bool | None = None,
        owns_verifier_evaluator: bool | None = None,
    ) -> None:
        self.settings = settings
        self._owns_llm_client = llm_client is None
        self._owns_critic_evaluator = (
            critic_evaluator is not None
            if owns_critic_evaluator is None
            else bool(owns_critic_evaluator)
        )
        self._owns_verifier_evaluator = (
            verifier_evaluator is not None
            if owns_verifier_evaluator is None
            else bool(owns_verifier_evaluator)
        )
        self.llm_client = (
            llm_client if llm_client is not None else LLMClient.from_settings(settings)
        )
        self.writer_llm = WriterLLM(self.llm_client)
        self.selector = BriefingSelector(settings)
        self.quality = BriefingQualityGate(settings)
        self.postprocessor = WriterPostprocessor(
            settings,
            writer_llm=self.writer_llm,
            priority_score=self.priority_score,
            char_limits=lambda mode, selected_count=None: self.char_limits(
                mode,
                selected_count=selected_count,
            ),
        )
        self.critic_evaluator = critic_evaluator
        self.verifier_evaluator = verifier_evaluator
        self.comfyui = ComfyUIClient(
            base_url=settings.comfyui_url,
            timeout=settings.comfyui_timeout,
            poll_interval=settings.comfyui_poll_interval,
        )

    async def close(self) -> None:
        """Release resources owned by this writer service."""
        await self.writer_llm.close()
        if self._owns_critic_evaluator and self.critic_evaluator is not None:
            await self.critic_evaluator.close()
        if self._owns_verifier_evaluator and self.verifier_evaluator is not None:
            await self.verifier_evaluator.close()
        await self.comfyui.close()
        if self._owns_llm_client:
            await self.llm_client.close()

    async def get_trace_metadata(self) -> WriterTraceMetadata:
        """Return writer model/prompt metadata for generation traces."""
        model = (self.llm_client.model_for_role("writer") or "").strip()
        return WriterTraceMetadata(
            llm_model=model,
            llm_model_version=self._model_version(model),
            prompts=self.writer_llm.prompt_versions(),
        )

    @staticmethod
    def _model_version(model: str) -> str:
        """Extract a coarse model version suffix for trace metadata."""
        normalized = str(model or "").strip()
        if ":" in normalized:
            return normalized.rsplit(":", 1)[-1].strip() or "unknown"
        if "@" in normalized:
            return normalized.rsplit("@", 1)[-1].strip() or "unknown"
        return "unknown"

    async def select_items_for_workflow(
        self,
        item_dicts: list[dict[str, Any]],
        workflow_mode: str,
        recent_published: list[dict[str, Any]] | None = None,
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

        quiet_mode = self.is_quiet_day(item_dicts)
        mode = "quiet_day" if quiet_mode else "standard"
        selected = self.select_items_for_briefing(
            item_dicts,
            recent_published=list(recent_published or []),
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
        if self.critic_evaluator is None:
            raise RuntimeError(
                "WriterService requires a critic_evaluator when critique is enabled."
            )
        return await self.critic_evaluator.evaluate(
            CritiqueRequest(
                draft_markdown=draft_markdown,
                items=tuple(items),
                mode=mode,
                source="writer_service",
                recent_briefings=tuple(recent_briefings or []),
                gate_hard_issues=tuple(gate_hard_issues or []),
                gate_soft_issues=tuple(gate_soft_issues or []),
            )
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
        if self.verifier_evaluator is None:
            raise RuntimeError(
                "WriterService requires a verifier_evaluator when verification is enabled."
            )
        return await self.verifier_evaluator.evaluate(
            VerificationRequest(
                draft_markdown=markdown,
                items=tuple(selected_items),
                mode=mode,
                source="writer_service",
            )
        )

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
            "critic_threshold_passed": critique_passed,
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
        return await run_generate_release_candidate(
            self,
            selected_items=selected_items,
            history=history,
            mode=mode,
        )

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
        return await run_simulate_briefing_body(
            self,
            items,
            recent_briefings,
            apply_critic_rewrites=apply_critic_rewrites,
        )

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

    @staticmethod
    def _default_verifier() -> dict[str, object]:
        """Return the permissive verifier payload used in simulation rewrites."""
        return _default_verifier()

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
        return await self.postprocessor.postprocess_briefing(
            briefing_body=briefing_body,
            selected_items=selected_items,
            mode=mode,
            min_chars=min_chars,
            target_chars=target_chars,
            hard_max_chars=hard_max_chars,
        )

    @staticmethod
    def dedupe_markdown_links(markdown: str) -> str:
        """Remove duplicate markdown links while preserving surrounding text."""
        return dedupe_markdown_links(markdown)

    @staticmethod
    def normalize_section_headings(markdown: str) -> str:
        """Normalize digest section headings to the house style."""
        return normalize_section_headings(markdown)

    @staticmethod
    def de_template_fields(markdown: str) -> str:
        """Strip template filler fields left by the model."""
        return de_template_fields(markdown)

    @staticmethod
    def missing_items_for_markdown(markdown: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return selected items whose URLs are missing from markdown."""
        return missing_items_for_markdown(markdown, items)

    @staticmethod
    def append_missing_items_section(
        markdown: str,
        missing_items: list[dict[str, Any]],
    ) -> str:
        """Append a deterministic references section for missing URLs."""
        return append_missing_items_section(markdown, missing_items)

    @staticmethod
    def clip_markdown(markdown: str, limit: int) -> str:
        """Clip markdown to a hard size limit while preserving readability."""
        return clip_markdown(markdown, limit)

    def enforce_release_link_hygiene(
        self,
        markdown: str,
        selected_items: list[dict[str, Any]],
        *,
        hard_max_chars: int,
    ) -> str:
        """Apply deterministic URL cleanup just before release checks."""
        return self.postprocessor.enforce_release_link_hygiene(
            markdown,
            selected_items,
            hard_max_chars=hard_max_chars,
        )

    @staticmethod
    def strip_unselected_markdown_links(
        markdown: str,
        selected_items: list[dict[str, Any]],
    ) -> str:
        """Drop markdown-link formatting for any URL outside the selected item set."""
        return WriterPostprocessor.strip_unselected_markdown_links(
            markdown,
            selected_items,
        )

    @staticmethod
    def strip_unselected_github_advisory_links(
        markdown: str,
        selected_items: list[dict[str, Any]],
    ) -> str:
        """Drop markdown-link formatting for GHSA URLs that were not selected."""
        return WriterPostprocessor.strip_unselected_github_advisory_links(
            markdown,
            selected_items,
        )

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
        return format_markdown(briefing_body, cover_url, mode=mode)

    @staticmethod
    def format_html(
        briefing_body: str,
        cover_url: str,
        *,
        mode: str = "standard",
    ) -> str:
        """Convert briefing markdown-ish text to styled HTML email markup."""
        return format_html(briefing_body, cover_url, mode=mode)

    @staticmethod
    def render_html_body(markdown: str) -> str:
        """Render markdown-ish digest text into readable HTML blocks."""
        return render_html_body(markdown)

    @staticmethod
    def inline_markdown_to_html(value: str) -> str:
        """Convert basic inline markdown syntax to safe HTML."""
        return inline_markdown_to_html(value)


WriterWorkflowProtocol = WriterWorkflow


__all__ = [
    "CriticEvaluator",
    "PostprocessedBriefing",
    "VerificationEvaluator",
    "WriterService",
    "WriterTraceMetadata",
    "WriterWorkflow",
    "WriterWorkflowProtocol",
]
