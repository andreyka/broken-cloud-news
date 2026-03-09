"""Structured analyst request/response contracts for remote services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bcn.common.models import AnalyzedItemUpdate


@dataclass(frozen=True)
class AnalystItemRequest:
    """Request to analyze one collected item."""

    item: dict[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return {"item": dict(self.item)}

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> AnalystItemRequest:
        data = payload or {}
        item = data.get("item")
        return cls(item=dict(item) if isinstance(item, dict) else {})


def analyzed_item_to_payload(update: AnalyzedItemUpdate) -> dict[str, Any]:
    """Render one analysis update as a JSON-safe payload."""
    return update.model_dump(mode="json")


def analyzed_item_from_payload(payload: dict[str, Any] | None) -> AnalyzedItemUpdate:
    """Parse one analysis update from a decoded payload."""
    return AnalyzedItemUpdate.model_validate(payload or {})


__all__ = [
    "AnalystItemRequest",
    "analyzed_item_from_payload",
    "analyzed_item_to_payload",
]
