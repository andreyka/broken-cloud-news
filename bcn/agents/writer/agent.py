"""Legacy writer agent wrapper over the control-plane generation service."""

from __future__ import annotations

from typing import Any

from a2a.server.agent_execution import AgentExecutor
from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from a2a.types import AgentSkill
from a2a.utils import new_agent_text_message
from typing_extensions import override

from bcn.agents.base import enqueue_event_safe
from bcn.agents.writer.service import WriterService
from bcn.common.config import Settings
from bcn.workflows.generation import REGULAR_DAILY_BRIEFING_MODE
from bcn.workflows.generation import REGULAR_MONTHLY_NEWSLETTER_MODE
from bcn.workflows.generation import execute_generation

_SUPPORTED_WORKFLOW_MODES = frozenset(
    (
        REGULAR_DAILY_BRIEFING_MODE,
        "ad_hoc",
        REGULAR_MONTHLY_NEWSLETTER_MODE,
    )
)

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
    """A2A wrapper that delegates generation to the control plane."""

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
        """Run the legacy writer agent through the control-plane service."""
        workflow_mode = self._resolve_workflow_mode(context.get_user_input() or "")
        message = await execute_generation(
            self.settings,
            mode=workflow_mode,
            writer_service=self.service,
            source="writer_agent",
            manage_pool=False,
        )
        await enqueue_event_safe(event_queue, new_agent_text_message(message))

    async def close(self) -> None:
        """Release writer resources."""
        await self.service.close()

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

    def _select_items_for_briefing(
        self,
        items: list[dict[str, Any]],
        recent_published: list[dict[str, Any]] | None = None,
        *,
        quiet_mode: bool = False,
    ) -> list[dict[str, Any]]:
        return self.service.select_items_for_briefing(
            items=items,
            recent_published=recent_published,
            quiet_mode=quiet_mode,
        )

    def _priority_score(
        self,
        item: dict[str, Any],
        recent_published: list[dict[str, Any]] | None = None,
    ) -> float:
        return self.service.priority_score(item, recent_published)

    def _passes_source_floor(self, item: dict[str, Any]) -> bool:
        return self.service.passes_source_floor(item)

    def _is_quiet_day(self, items: list[dict[str, Any]]) -> bool:
        return self.service.is_quiet_day(items)

    def _select_items_for_monthly_newsletter(
        self,
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return self.service.select_items_for_monthly_newsletter(items)

    def _char_limits(
        self,
        mode: str,
        selected_count: int | None = None,
    ) -> tuple[int, int, int]:
        return self.service.char_limits(mode, selected_count=selected_count)

    def _quality_gate(
        self,
        markdown: str,
        selected_items: list[dict[str, Any]],
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
        selected_items: list[dict[str, Any]],
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
    def _missing_items_for_markdown(markdown: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return WriterService.missing_items_for_markdown(markdown, items)

    @staticmethod
    def _append_missing_items_section(
        markdown: str,
        missing_items: list[dict[str, Any]],
    ) -> str:
        return WriterService.append_missing_items_section(markdown, missing_items)

    @staticmethod
    def _clip_markdown(markdown: str, limit: int) -> str:
        return WriterService.clip_markdown(markdown, limit)

    def _enforce_release_link_hygiene(
        self,
        markdown: str,
        selected_items: list[dict[str, Any]],
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
        markdown: str,
        selected_items: list[dict[str, Any]],
    ) -> str:
        return WriterService.strip_unselected_github_advisory_links(
            markdown,
            selected_items,
        )

    @staticmethod
    def _strip_unselected_markdown_links(
        markdown: str,
        selected_items: list[dict[str, Any]],
    ) -> str:
        return WriterService.strip_unselected_markdown_links(
            markdown,
            selected_items,
        )

    @override
    async def cancel(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Cancel is not supported."""
        raise NotImplementedError("cancel not supported")
