"""Verifier domain service shared by the control plane and transport adapters."""

from __future__ import annotations

import logging
from typing import Any

from bcn.briefing.verifier import BriefingFactVerifier
from bcn.common.config import Settings
from bcn.contracts.review import VerificationRequest
from bcn.contracts.review import parse_verification_request_payload
from bcn.contracts.review import render_verification_request_payload

logger = logging.getLogger(__name__)


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
