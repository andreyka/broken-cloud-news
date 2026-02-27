"""Verifier LLM interactions."""

import json
import logging
from typing import Any

from bcn.agents.verifier.prompt import BRIEFING_FACT_VERIFIER_PROMPT
from bcn.common.llm import LLMClient

logger = logging.getLogger(__name__)


class VerifierLLM:

    def __init__(self, client: LLMClient):
        self.client = client

    async def verify_briefing_facts(
        self,
        draft_markdown: str,
        items: list[dict],
        *,
        mode: str = "standard",
        deterministic_issues: list[str] | None = None,
    ) -> dict[str, Any]:
        """Verify factual grounding of a draft against provided items."""
        item_lines = [
            (f"- [{item.get('source_type', '')}] {item.get('title', '')}\n"
             f"  URL: {item.get('url', '')}\n"
             f"  Summary: {item.get('summary', '')}") for item in items
        ]
        mode_text = "quiet_day" if mode == "quiet_day" else "standard"
        det = "\n".join(
            f"- {line}" for line in (deterministic_issues or [])) or "- none"
        user_msg = (f"Mode: {mode_text}\n\n"
                    f"Selected items ({len(items)}):\n" +
                    "\n".join(item_lines) +
                    "\n\nDeterministic verifier findings:\n" + det +
                    "\n\nDraft:\n" + draft_markdown)
        raw = await self.client.chat_for_role(
            role="verifier",
            system_prompt=BRIEFING_FACT_VERIFIER_PROMPT,
            user_content=user_msg,
            json_response=True,
        )
        try:
            parsed = self.client.parse_json_response(raw)
            if not isinstance(parsed, dict):
                raise ValueError("verifier payload must be an object")
            hard = parsed.get("hard_issues", [])
            soft = parsed.get("soft_issues", [])
            recs = parsed.get("recommendations", [])
            if not isinstance(hard, list):
                hard = [str(hard)]
            if not isinstance(soft, list):
                soft = [str(soft)]
            if not isinstance(recs, list):
                recs = [str(recs)]
            return {
                "passed": bool(parsed.get("passed", False)),
                "score": max(0, min(100, int(parsed.get("score", 0)))),
                "hard_issues": [str(i) for i in hard[:12]],
                "soft_issues": [str(i) for i in soft[:12]],
                "recommendations": [str(i) for i in recs[:12]],
            }
        except Exception:
            logger.warning("Failed to parse verifier JSON, using fallback")
            return {
                "passed":
                    False,
                "score":
                    0,
                "hard_issues": ["Verifier response parsing failed"],
                "soft_issues": [],
                "recommendations": [
                    "Re-run factual verification and tighten claims to source evidence."
                ],
            }
