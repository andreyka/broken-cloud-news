"""Structured collector request/response contracts for remote services."""

from __future__ import annotations

from dataclasses import dataclass

from bcn.common.models import CollectedNewsItem

_VALID_COLLECTION_SOURCES = frozenset({"ghsa", "rss", "twitter", "reddit"})


@dataclass(frozen=True)
class CollectorSourceRequest:
    """Request to collect items from exactly one upstream source."""

    source: str

    def to_payload(self) -> dict[str, object]:
        return {"source": self.source}

    @classmethod
    def from_payload(cls, payload: dict[str, object] | None) -> CollectorSourceRequest:
        data = payload or {}
        return cls(source=str(data.get("source") or "").strip().lower())


def collector_items_to_payload(items: list[CollectedNewsItem]) -> dict[str, object]:
    """Render collected items as a JSON-safe transport payload."""
    return {"items": [item.model_dump(mode="json") for item in items]}


def collector_items_from_payload(payload: dict[str, object] | None) -> list[CollectedNewsItem]:
    """Parse collected items from a decoded transport payload."""
    data = payload or {}
    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        return []
    items: list[CollectedNewsItem] = []
    for item in raw_items:
        if isinstance(item, dict):
            items.append(CollectedNewsItem.model_validate(item))
    return items


def validate_collection_source(source: str) -> str:
    """Return a normalized collection source or raise for invalid input."""
    normalized = str(source or "").strip().lower()
    if normalized not in _VALID_COLLECTION_SOURCES:
        raise ValueError("source must be one of: ghsa, rss, twitter, reddit.")
    return normalized


__all__ = [
    "CollectorSourceRequest",
    "collector_items_from_payload",
    "collector_items_to_payload",
    "validate_collection_source",
]
