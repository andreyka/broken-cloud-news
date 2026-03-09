"""Control-plane critique and verification services for explicit review input."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from bcn.common.config import Settings
from bcn.contracts.review import CritiqueRequest
from bcn.contracts.review import VerificationRequest
from bcn.contracts.services import CriticEvaluator
from bcn.contracts.services import VerificationEvaluator
from bcn.persistence.briefings import get_latest_any_briefing
from bcn.persistence.news_items import get_items_by_ids
from bcn.persistence.runtime import close_pool
from bcn.persistence.runtime import get_pool
from bcn.service_registry import build_critic_evaluator
from bcn.service_registry import build_verifier_evaluator

logger = logging.getLogger(__name__)


async def _latest_briefing_request_kwargs() -> tuple[dict[str, object] | None, list[dict]]:
    """Return latest briefing row and its selected items."""
    briefing = await get_latest_any_briefing()
    if not briefing:
        return None, []

    item_ids = list(briefing.get("item_ids") or [])
    items = [dict(row) for row in await get_items_by_ids(item_ids)] if item_ids else []
    return dict(briefing), items


async def execute_critique(
    settings: Settings,
    *,
    latest: bool = False,
    file_path: str | None = None,
    text_input: str | None = None,
    markdown: str | None = None,
    critic_service: CriticEvaluator | None = None,
    source: str = "workflow_service",
    manage_pool: bool = True,
) -> str:
    """Resolve review input, run critique, and return JSON output."""
    await get_pool(settings)
    active_service = critic_service or build_critic_evaluator(settings)
    owns_service = critic_service is None

    try:
        request: CritiqueRequest | None = None
        resolved_markdown = markdown
        if text_input:
            resolved_markdown = text_input
        elif file_path:
            resolved_markdown = Path(file_path).read_text(encoding="utf-8")

        if resolved_markdown:
            request = CritiqueRequest(
                draft_markdown=resolved_markdown,
                source=source,
            )
        elif latest or not resolved_markdown:
            briefing, items = await _latest_briefing_request_kwargs()
            if briefing is None:
                return "No briefing found to critique"
            request = CritiqueRequest(
                draft_markdown=str(briefing.get("content_markdown") or ""),
                items=tuple(items),
                source=f"briefing:{briefing['id']}",
            )

        if request is None or not request.draft_markdown.strip():
            return "No markdown provided for critique"

        result = await active_service.evaluate(request)
        logger.info("Critique complete [source=%s origin=%s]", result["source"], source)
        return json.dumps(result, ensure_ascii=False, indent=2)
    finally:
        if owns_service:
            await active_service.close()
        if manage_pool:
            await close_pool()


async def execute_verification(
    settings: Settings,
    *,
    latest: bool = False,
    file_path: str | None = None,
    text_input: str | None = None,
    markdown: str | None = None,
    verifier_service: VerificationEvaluator | None = None,
    source: str = "workflow_service",
    manage_pool: bool = True,
) -> str:
    """Resolve review input, run verification, and return JSON output."""
    await get_pool(settings)
    active_service = verifier_service or build_verifier_evaluator(settings)
    owns_service = verifier_service is None

    try:
        request: VerificationRequest | None = None
        resolved_markdown = markdown
        if text_input:
            resolved_markdown = text_input
        elif file_path:
            resolved_markdown = Path(file_path).read_text(encoding="utf-8")

        if resolved_markdown:
            request = VerificationRequest(
                draft_markdown=resolved_markdown,
                source=source,
            )
        elif latest or not resolved_markdown:
            briefing, items = await _latest_briefing_request_kwargs()
            if briefing is None:
                return "No briefing found to verify"
            request = VerificationRequest(
                draft_markdown=str(briefing.get("content_markdown") or ""),
                items=tuple(items),
                source=f"briefing:{briefing['id']}",
            )

        if request is None or not request.draft_markdown.strip():
            return "No markdown provided for verification"

        result = await active_service.evaluate(request)
        logger.info(
            "Verification complete [source=%s origin=%s]",
            result["source"],
            source,
        )
        return json.dumps(result, ensure_ascii=False, indent=2)
    finally:
        if owns_service:
            await active_service.close()
        if manage_pool:
            await close_pool()


__all__ = [
    "execute_critique",
    "execute_verification",
]
