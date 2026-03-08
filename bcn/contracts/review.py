"""Explicit review contracts used across control-plane and transport layers."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

_CRITIQUE_REQUEST_PREFIX = "critique_briefing::"
_VERIFICATION_REQUEST_PREFIX = "verify_briefing::"


@dataclass(frozen=True)
class CritiqueRequest:
    """Explicit critic input prepared by the control plane."""

    draft_markdown: str
    items: tuple[dict[str, Any], ...] = ()
    mode: str = "standard"
    source: str = "input"
    recent_briefings: tuple[dict[str, Any], ...] = ()
    gate_hard_issues: tuple[str, ...] = ()
    gate_soft_issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerificationRequest:
    """Explicit verifier input prepared by the control plane."""

    draft_markdown: str
    items: tuple[dict[str, Any], ...] = ()
    mode: str = "standard"
    source: str = "input"


def render_critique_request_payload(request: CritiqueRequest) -> str:
    """Render a structured critique request for adapter transport."""
    payload = {
        "draft_markdown": request.draft_markdown,
        "gate_hard_issues": list(request.gate_hard_issues),
        "gate_soft_issues": list(request.gate_soft_issues),
        "items": list(request.items),
        "mode": str(request.mode or "standard"),
        "recent_briefings": list(request.recent_briefings),
        "source": str(request.source or "input"),
    }
    return _CRITIQUE_REQUEST_PREFIX + json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )


def parse_critique_request_payload(text: str) -> CritiqueRequest | None:
    """Parse a structured critique request from adapter input text."""
    raw_text = str(text or "").strip()
    if not raw_text or raw_text.lower() == "critique_latest":
        return None
    if raw_text.startswith("critique_markdown::"):
        return CritiqueRequest(
            draft_markdown=raw_text.split("::", 1)[1].strip(),
        )
    if not raw_text.startswith(_CRITIQUE_REQUEST_PREFIX):
        return CritiqueRequest(draft_markdown=raw_text)

    raw_payload = raw_text[len(_CRITIQUE_REQUEST_PREFIX) :].strip()
    if not raw_payload:
        return None

    try:
        decoded = json.loads(raw_payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None

    draft_markdown = str(decoded.get("draft_markdown") or "").strip()
    if not draft_markdown:
        return None

    items_raw = decoded.get("items", [])
    recent_raw = decoded.get("recent_briefings", [])
    gate_hard_raw = decoded.get("gate_hard_issues", [])
    gate_soft_raw = decoded.get("gate_soft_issues", [])

    return CritiqueRequest(
        draft_markdown=draft_markdown,
        items=tuple(item for item in items_raw if isinstance(item, dict)),
        mode=str(decoded.get("mode") or "standard").strip() or "standard",
        source=str(decoded.get("source") or "input").strip() or "input",
        recent_briefings=tuple(item for item in recent_raw if isinstance(item, dict)),
        gate_hard_issues=tuple(
            str(item).strip() for item in gate_hard_raw if str(item).strip()
        ),
        gate_soft_issues=tuple(
            str(item).strip() for item in gate_soft_raw if str(item).strip()
        ),
    )


def render_verification_request_payload(request: VerificationRequest) -> str:
    """Render a structured verification request for adapter transport."""
    payload = {
        "draft_markdown": request.draft_markdown,
        "items": list(request.items),
        "mode": str(request.mode or "standard"),
        "source": str(request.source or "input"),
    }
    return _VERIFICATION_REQUEST_PREFIX + json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )


def parse_verification_request_payload(text: str) -> VerificationRequest | None:
    """Parse a structured verification request from adapter input text."""
    raw_text = str(text or "").strip()
    if not raw_text or raw_text.lower() == "verify_latest":
        return None
    if raw_text.startswith("verify_markdown::"):
        return VerificationRequest(
            draft_markdown=raw_text.split("::", 1)[1].strip(),
        )
    if not raw_text.startswith(_VERIFICATION_REQUEST_PREFIX):
        return VerificationRequest(draft_markdown=raw_text)

    raw_payload = raw_text[len(_VERIFICATION_REQUEST_PREFIX) :].strip()
    if not raw_payload:
        return None

    try:
        decoded = json.loads(raw_payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None

    draft_markdown = str(decoded.get("draft_markdown") or "").strip()
    if not draft_markdown:
        return None

    items_raw = decoded.get("items", [])
    return VerificationRequest(
        draft_markdown=draft_markdown,
        items=tuple(item for item in items_raw if isinstance(item, dict)),
        mode=str(decoded.get("mode") or "standard").strip() or "standard",
        source=str(decoded.get("source") or "input").strip() or "input",
    )


__all__ = [
    "CritiqueRequest",
    "VerificationRequest",
    "parse_critique_request_payload",
    "parse_verification_request_payload",
    "render_critique_request_payload",
    "render_verification_request_payload",
]
