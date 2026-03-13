"""Writer domain service used by generation workflows and evaluation lanes."""

from __future__ import annotations

import logging
from typing import Any

from bcn.briefing import BriefingQualityGate
from bcn.briefing import BriefingSelector
from bcn.common.comfyui import ComfyUIClient
from bcn.common.config import Settings
from bcn.common.llm import LLMClient
from bcn.common.prompt_overrides import load_json_prompt_bundle
from bcn.contracts.services import CriticEvaluator
from bcn.contracts.services import VerificationEvaluator
from bcn.contracts.services import WriterTraceMetadata
from bcn.contracts.services import WriterWorkflow
from bcn.services.writer.covers import build_release_artifact as run_build_release_artifact
from bcn.services.writer.drafting import (
    generate_release_candidate as run_generate_release_candidate,
)
from bcn.services.writer.drafting import (
    simulate_briefing_body as run_simulate_briefing_body,
)
from bcn.services.writer.llm import WriterLLM
from bcn.services.writer.models import PostprocessedBriefing
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
from bcn.services.writer.review import build_preference_rationale
from bcn.services.writer.review import build_rewrite_feedback_context
from bcn.services.writer.review import critique_markdown
from bcn.services.writer.review import default_verifier
from bcn.services.writer.review import evaluate_existing_markdown
from bcn.services.writer.review import extract_repeated_topics
from bcn.services.writer.review import extract_sticky_rewrite_constraints
from bcn.services.writer.review import has_critical_critic_issue
from bcn.services.writer.review import passes_critic_thresholds
from bcn.services.writer.review import quality_gate
from bcn.services.writer.review import string_list
from bcn.services.writer.review import trim_repeated_selected_items
from bcn.services.writer.review import verify_markdown
from bcn.services.writer.selection import char_limits
from bcn.services.writer.selection import is_quiet_day
from bcn.services.writer.selection import passes_source_floor
from bcn.services.writer.selection import priority_score
from bcn.services.writer.selection import select_items_for_briefing
from bcn.services.writer.selection import select_items_for_monthly_newsletter
from bcn.services.writer.selection import select_items_for_workflow

logger = logging.getLogger(__name__)


class WriterService:
    """Thin facade for writer selection, review, drafting, and artifact assembly."""

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
        self.writer_llm = WriterLLM(
            self.llm_client,
            prompts=load_json_prompt_bundle(settings.writer_prompt_bundle_path),
        )
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
        return select_items_for_workflow(
            self,
            item_dicts,
            workflow_mode,
            recent_published=recent_published,
        )

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
        return await critique_markdown(
            self,
            draft_markdown,
            items,
            mode=mode,
            recent_briefings=recent_briefings,
            gate_hard_issues=gate_hard_issues,
            gate_soft_issues=gate_soft_issues,
        )

    async def verify_markdown(
        self,
        markdown: str,
        selected_items: list[dict[str, Any]],
        *,
        mode: str,
    ) -> dict[str, Any]:
        """Run factual verification or return a permissive default payload."""
        return await verify_markdown(
            self,
            markdown,
            selected_items,
            mode=mode,
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
        return await evaluate_existing_markdown(
            self,
            markdown=markdown,
            selected_items=selected_items,
            history=history,
            mode=mode,
        )

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
        return await run_build_release_artifact(
            self,
            briefing_body=briefing_body,
            selected_items=selected_items,
            mode=mode,
        )

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
        return select_items_for_briefing(
            self,
            items,
            recent_published=recent_published,
            quiet_mode=quiet_mode,
        )

    def priority_score(
        self,
        item: dict[str, Any],
        recent_published: list[dict[str, Any]] | None = None,
    ) -> float:
        """Return the selector priority score for one item."""
        return priority_score(self, item, recent_published)

    def passes_source_floor(self, item: dict[str, Any]) -> bool:
        """Return whether an item clears the selector's source floor."""
        return passes_source_floor(self, item)

    def is_quiet_day(self, items: list[dict[str, Any]]) -> bool:
        """Return whether the pool should use quiet-day briefing mode."""
        return is_quiet_day(self, items)

    def select_items_for_monthly_newsletter(
        self,
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Select a broader, story-deduped set of items for monthly mode."""
        return select_items_for_monthly_newsletter(self, items)

    def char_limits(
        self,
        mode: str,
        *,
        selected_count: int | None = None,
    ) -> tuple[int, int, int]:
        """Return min/target/max body lengths for a given mode."""
        return char_limits(self, mode, selected_count=selected_count)

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
        return quality_gate(
            self,
            markdown,
            selected_items,
            mode=mode,
            min_chars=min_chars,
            hard_max_chars=hard_max_chars,
        )

    def passes_critic_thresholds(self, critique: dict[str, object]) -> bool:
        """Apply blocking thresholds for critic score and key dimensions."""
        return passes_critic_thresholds(self, critique)

    @staticmethod
    def has_critical_critic_issue(critique: dict[str, object]) -> bool:
        """Return whether the critic payload contains a blocking issue."""
        return has_critical_critic_issue(critique)

    @staticmethod
    def _default_verifier() -> dict[str, object]:
        """Return the permissive verifier payload used in simulation rewrites."""
        return default_verifier()

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
        sticky_constraints: list[str] | None = None,
    ) -> dict[str, Any]:
        """Build compact structured rewrite guidance for the LLM."""
        return build_rewrite_feedback_context(
            self,
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
            sticky_constraints=sticky_constraints,
        )

    @staticmethod
    def extract_repeated_topics(critique: dict[str, object]) -> list[str]:
        """Extract repeated-topic labels from the critic payload."""
        return extract_repeated_topics(critique)

    def trim_repeated_selected_items(
        self,
        *,
        selected_items: list[dict[str, Any]],
        critique: dict[str, object],
        history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Drop critic-flagged repeats when recent briefing history supports it."""
        return trim_repeated_selected_items(
            self,
            selected_items=selected_items,
            critique=critique,
            history=history,
        )

    @staticmethod
    def extract_sticky_rewrite_constraints(
        critique: dict[str, object],
        verification: dict[str, object],
    ) -> list[str]:
        """Extract persistent rewrite constraints from review findings."""
        return extract_sticky_rewrite_constraints(critique, verification)

    @staticmethod
    def build_preference_rationale(feedback: list[str] | None) -> str:
        """Summarize rewrite feedback for preference-pair training rows."""
        return build_preference_rationale(feedback)

    @staticmethod
    def string_list(value: object, *, limit: int = 16) -> list[str]:
        """Normalize arbitrary payload values into a short string list."""
        return string_list(value, limit=limit)

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
