"""LLM review wrapper for promotion of newly introduced collection sources."""

from __future__ import annotations

import json
import logging
from typing import Any

from bcn.services.collector.review_prompt import SOURCE_REVIEW_SYSTEM_PROMPT
from bcn.common.llm import LLMClient
from bcn.common.models import CollectedNewsItem
from bcn.common.models import CollectionSourceReview

logger = logging.getLogger(__name__)


class SourceReviewLLM:
    """Use the analyst role to review whether a new source should be promoted."""

    def __init__(self, client: LLMClient):
        self.client = client

    async def review_source(
        self,
        *,
        source_type: str,
        display_name: str,
        raw_config: dict[str, Any],
        sample_items: list[CollectedNewsItem],
    ) -> CollectionSourceReview:
        payload = {
            "source_type": source_type,
            "display_name": display_name,
            "raw_config": raw_config,
            "sample_items": [
                {
                    "title": item.title or "",
                    "url": item.url,
                    "published_at": str(item.published_at),
                    "summary": str(item.raw_data.get("summary") or "")[:400],
                    "excerpt": str(item.full_content or "")[:600],
                }
                for item in sample_items
            ],
        }
        raw = await self.client.chat_for_role(
            role="analyst",
            system_prompt=SOURCE_REVIEW_SYSTEM_PROMPT,
            user_content=json.dumps(payload, ensure_ascii=False, default=str),
            json_response=True,
        )
        try:
            parsed = self.client.parse_json_response(raw)
            return CollectionSourceReview(
                decision=str(parsed.get("decision") or "quarantine").strip().lower(),
                confidence=str(parsed.get("confidence") or "medium").strip().lower(),
                rationale=str(parsed.get("rationale") or "").strip(),
                signals=[
                    str(signal).strip()
                    for signal in parsed.get("signals", [])
                    if str(signal).strip()
                ],
            )
        except Exception as exc:
            logger.warning(
                "Failed to parse source review JSON, leaving source pending review"
            )
            raise ValueError(
                "LLM source review response was invalid JSON."
            ) from exc
