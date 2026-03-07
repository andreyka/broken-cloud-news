"""Verifier domain service shared by the control plane and legacy agent."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from typing import Any

from bcn.briefing.verifier import BriefingFactVerifier
from bcn.common.config import Settings

logger = logging.getLogger(__name__)

_VERIFICATION_REQUEST_PREFIX = "verify_briefing::"


@dataclass(frozen=True)
class VerificationRequest:
    """Explicit verifier input prepared by the control plane."""

    draft_markdown: str
    items: tuple[dict[str, Any], ...] = ()
    mode: str = "standard"
    source: str = "input"


def render_verification_request_payload(request: VerificationRequest) -> str:
    """Render a structured verification request for agent transport."""
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
    """Parse a structured verification request from agent input text."""
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


class VerifierService:
    """Domain service for explicit briefing verification without DB ownership."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.verifier = BriefingFactVerifier(settings)

    async def evaluate(self, request: VerificationRequest) -> dict[str, Any]:
        """Verify one explicit briefing payload."""
        report = await self.verifier.evaluate(
            request.draft_markdown,
            list(request.items),
            mode=request.mode,
        )
        result = {
            "source": request.source,
            "verifier_passed": bool(report.get("passed", False)),
            "verifier_score": int(report.get("score", 0) or 0),
            "issues": [str(item) for item in report.get("issues", [])],
            "recommendations": [
                str(item) for item in report.get("recommendations", [])
            ],
            "dead_urls": [str(item) for item in report.get("dead_urls", [])],
            "top_story_ok": bool(report.get("top_story_ok", True)),
        }
        logger.info(
            "Verifier done for %s: passed=%s score=%s",
            request.source,
            result["verifier_passed"],
            result["verifier_score"],
        )
        return result

    async def close(self) -> None:
        """Release resources owned by this verifier service."""
        await self.verifier.close()


__all__ = [
    "VerificationRequest",
    "VerifierService",
    "parse_verification_request_payload",
    "render_verification_request_payload",
]
