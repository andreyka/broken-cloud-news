"""Typed writer request contracts used by local and remote workflow adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _coerce_dict_list(value: object) -> list[dict[str, Any]]:
    """Return a JSON-like list of dict payloads."""
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


@dataclass(frozen=True)
class WriterSelectionRequest:
    """Side-effect-free selection request for one workflow mode."""

    item_dicts: list[dict[str, Any]]
    workflow_mode: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "item_dicts": list(self.item_dicts),
            "workflow_mode": self.workflow_mode,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> WriterSelectionRequest:
        data = payload or {}
        return cls(
            item_dicts=_coerce_dict_list(data.get("item_dicts")),
            workflow_mode=str(data.get("workflow_mode") or "").strip(),
        )


@dataclass(frozen=True)
class WriterDraftEvaluationRequest:
    """Request to score an existing markdown draft."""

    markdown: str
    selected_items: list[dict[str, Any]]
    history: list[dict[str, Any]]
    mode: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "markdown": self.markdown,
            "selected_items": list(self.selected_items),
            "history": list(self.history),
            "mode": self.mode,
        }

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any] | None,
    ) -> WriterDraftEvaluationRequest:
        data = payload or {}
        return cls(
            markdown=str(data.get("markdown") or ""),
            selected_items=_coerce_dict_list(data.get("selected_items")),
            history=_coerce_dict_list(data.get("history")),
            mode=str(data.get("mode") or "").strip(),
        )


@dataclass(frozen=True)
class WriterReleaseCandidateRequest:
    """Request to generate a non-publishing release candidate."""

    selected_items: list[dict[str, Any]]
    history: list[dict[str, Any]]
    mode: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "selected_items": list(self.selected_items),
            "history": list(self.history),
            "mode": self.mode,
        }

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any] | None,
    ) -> WriterReleaseCandidateRequest:
        data = payload or {}
        return cls(
            selected_items=_coerce_dict_list(data.get("selected_items")),
            history=_coerce_dict_list(data.get("history")),
            mode=str(data.get("mode") or "").strip(),
        )


@dataclass(frozen=True)
class WriterArtifactRequest:
    """Request to render final publishable briefing assets."""

    briefing_body: str
    selected_items: list[dict[str, Any]]
    mode: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "briefing_body": self.briefing_body,
            "selected_items": list(self.selected_items),
            "mode": self.mode,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> WriterArtifactRequest:
        data = payload or {}
        return cls(
            briefing_body=str(data.get("briefing_body") or ""),
            selected_items=_coerce_dict_list(data.get("selected_items")),
            mode=str(data.get("mode") or "").strip(),
        )


@dataclass(frozen=True)
class WriterSimulationRequest:
    """Request to simulate a briefing body for replay/evaluation."""

    items: list[dict[str, Any]]
    recent_briefings: list[dict[str, Any]]
    apply_critic_rewrites: bool

    def to_payload(self) -> dict[str, Any]:
        return {
            "items": list(self.items),
            "recent_briefings": list(self.recent_briefings),
            "apply_critic_rewrites": bool(self.apply_critic_rewrites),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> WriterSimulationRequest:
        data = payload or {}
        return cls(
            items=_coerce_dict_list(data.get("items")),
            recent_briefings=_coerce_dict_list(data.get("recent_briefings")),
            apply_critic_rewrites=bool(data.get("apply_critic_rewrites", False)),
        )

