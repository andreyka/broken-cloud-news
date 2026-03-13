"""Shared settings override helpers for evaluation and optimization."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bcn.common.config import Settings


def load_settings_with_overrides(
    base_settings: Settings,
    overrides_path: str | None = None,
) -> tuple[Settings, dict[str, Any]]:
    """Return validated settings merged with optional JSON overrides."""
    if not overrides_path:
        return base_settings, {}

    raw = Path(overrides_path).read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Settings overrides must be a JSON object.")

    merged = dict(base_settings.model_dump())
    for key, value in payload.items():
        merged[str(key)] = value
    return Settings(**merged), {str(k): v for k, v in payload.items()}
