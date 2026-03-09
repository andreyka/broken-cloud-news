"""Typed service contracts shared by the control plane and remote adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from typing import TYPE_CHECKING
from typing import Protocol

from bcn.common.models import AnalyzedItemUpdate
from bcn.common.models import CollectedNewsItem
from bcn.contracts.review import CritiqueRequest
from bcn.contracts.review import VerificationRequest

if TYPE_CHECKING:
    from bcn.contracts.distributor import DeliveryRequest
    from bcn.contracts.distributor import DeliveryResult


@dataclass(frozen=True)
class WriterTraceMetadata:
    """Writer model/prompt metadata persisted in generation traces."""

    llm_model: str
    llm_model_version: str
    prompts: dict[str, Any]

    def to_payload(self) -> dict[str, Any]:
        """Render metadata as a JSON-safe payload."""
        return {
            "llm_model": self.llm_model,
            "llm_model_version": self.llm_model_version,
            "prompts": dict(self.prompts),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> WriterTraceMetadata:
        """Build one metadata instance from a transport payload."""
        data = payload or {}
        prompts = data.get("prompts")
        return cls(
            llm_model=str(data.get("llm_model") or ""),
            llm_model_version=str(data.get("llm_model_version") or ""),
            prompts=dict(prompts) if isinstance(prompts, dict) else {},
        )


class CriticEvaluator(Protocol):
    """Critic interface used regardless of local or remote deployment."""

    async def evaluate(self, request: CritiqueRequest) -> dict[str, Any]:
        """Evaluate one critique request."""

    async def close(self) -> None:
        """Release owned resources."""


class VerificationEvaluator(Protocol):
    """Verifier interface used regardless of local or remote deployment."""

    async def evaluate(self, request: VerificationRequest) -> dict[str, Any]:
        """Evaluate one verification request."""

    async def close(self) -> None:
        """Release owned resources."""


class CollectorWorkflow(Protocol):
    """Collector interface used by the control plane and remote adapters."""

    async def close(self) -> None:
        """Release any resources held by the collector workflow service."""

    async def collect(self, source: str) -> list[CollectedNewsItem]:
        """Collect items for one source without persisting them."""


class AnalystWorkflow(Protocol):
    """Analyst interface used by the control plane and remote adapters."""

    async def close(self) -> None:
        """Release any resources held by the analyst workflow service."""

    async def analyze_item(self, item: dict[str, Any]) -> AnalyzedItemUpdate:
        """Analyze one collected item and return the persistable update."""


class DistributorWorkflow(Protocol):
    """Distributor interface used by the control plane and remote adapters."""

    async def close(self) -> None:
        """Release any resources held by the distributor workflow service."""

    async def deliver(self, request: "DeliveryRequest") -> "DeliveryResult":
        """Deliver one briefing payload through the configured channels."""


class WriterWorkflow(Protocol):
    """Writer interface used by workflows and evaluation lanes."""

    async def close(self) -> None:
        """Release any resources held by the writer workflow service."""

    async def get_trace_metadata(self) -> WriterTraceMetadata:
        """Return writer trace metadata for generation persistence."""

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

    async def build_release_artifact(
        self,
        *,
        briefing_body: str,
        selected_items: list[dict[str, Any]],
        mode: str,
    ) -> dict[str, str]:
        """Build final publishable markdown/html assets."""

    async def simulate_briefing_body(
        self,
        items: list[dict[str, Any]],
        recent_briefings: list[dict[str, Any]],
        *,
        apply_critic_rewrites: bool,
    ) -> tuple[str, dict[str, object]]:
        """Generate a replay candidate body without inserting a briefing row."""
