"""Shared writer data structures."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PostprocessedBriefing:
    """Finalized draft body plus the selected items it actually covers."""

    markdown: str
    selected_items: list[dict[str, Any]]


__all__ = ["PostprocessedBriefing"]
