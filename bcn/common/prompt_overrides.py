"""Helpers for loading optional prompt overrides from disk."""

from __future__ import annotations

import json
from pathlib import Path


def _read_text(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8").strip()


def load_text_prompt_override(path: str | Path | None) -> str | None:
    """Load one text prompt override from disk, if configured."""
    if not path:
        return None
    text = _read_text(path)
    return text or None


def load_json_prompt_bundle(path: str | Path | None) -> dict[str, str]:
    """Load a JSON object mapping prompt keys to override text."""
    if not path:
        return {}
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Prompt bundle override must be a JSON object.")
    out: dict[str, str] = {}
    for key, value in raw.items():
        text = str(value or "").strip()
        if text:
            out[str(key)] = text
    return out
