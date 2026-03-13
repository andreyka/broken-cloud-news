"""Variant loading helpers for offline optimization runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_PATH_KEYS = {
    "writer_prompt_bundle_path",
    "critic_prompt_path",
    "verifier_prompt_path",
}


def load_variant_spec(path: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load and normalize one optimization variant spec."""
    variant_path = Path(path).resolve()
    payload = json.loads(variant_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Variant file must be a JSON object.")

    variant_id = str(payload.get("id") or variant_path.stem).strip()
    if not variant_id:
        raise ValueError("Variant id is required.")

    settings_overrides = payload.get("settings_overrides") or {}
    if not isinstance(settings_overrides, dict):
        raise ValueError("settings_overrides must be a JSON object.")

    normalized: dict[str, Any] = {}
    for key, value in settings_overrides.items():
        key_text = str(key)
        if key_text in _PATH_KEYS and str(value or "").strip():
            normalized[key_text] = str((variant_path.parent / str(value)).resolve())
        else:
            normalized[key_text] = value

    spec = {
        "id": variant_id,
        "description": str(payload.get("description") or "").strip(),
        "base": str(payload.get("base") or "champion").strip() or "champion",
        "settings_overrides": normalized,
    }
    return spec, normalized
