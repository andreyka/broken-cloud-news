"""Critic LLM interactions."""

import logging
import re
from typing import Any

from bcn.agents.critic.prompt import BRIEFING_CRITIC_PROMPT
from bcn.common.llm import LLMClient

logger = logging.getLogger(__name__)


class CriticLLM:
    def __init__(self, client: LLMClient):
        self.client = client

    async def critique_briefing(
        self,
        draft_markdown: str,
        items: list[dict],
        *,
        mode: str = "standard",
        gate_hard_issues: list[str] | None = None,
        gate_soft_issues: list[str] | None = None,
        recent_briefings: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Critique a draft briefing and return structured pass/fail guidance."""
        item_lines = [
            f"- [{item.get('source_type', '')}] {item.get('title', '')} :: {item.get('url', '')}"
            for item in items
        ]
        hard_text = (
            "\n".join(f"- {issue}" for issue in (gate_hard_issues or [])) or "- none"
        )
        soft_text = (
            "\n".join(f"- {issue}" for issue in (gate_soft_issues or [])) or "- none"
        )
        mode_text = "quiet_day" if mode == "quiet_day" else "standard"

        # Build history context for novelty checking
        history_block = ""
        if recent_briefings:
            history_parts: list[str] = []
            for idx, briefing in enumerate(recent_briefings[:10], start=1):
                body = briefing.get("content_markdown", "") or ""
                urls = re.findall(r"\[.*?\]\((https?://[^\)]+)\)", body)
                topics = re.findall(r"\*\*(.+?)\*\*", body)
                cves = re.findall(r"CVE-\d{4}-\d+", body, flags=re.IGNORECASE)
                parts = []
                if topics:
                    parts.append(f"topics={topics[:5]}")
                if urls:
                    parts.append(f"urls={urls[:8]}")
                if cves:
                    parts.append(f"cves={cves}")
                if parts:
                    history_parts.append(f"{idx}) " + " | ".join(parts))
            if history_parts:
                history_block = (
                    "\n\nRecent briefing history (check for repeated topics/URLs):\n"
                    + "\n".join(history_parts)
                )

        user_msg = (
            f"Mode: {mode_text}\n"
            f"Selected items ({len(items)}):\n"
            + "\n".join(item_lines)
            + "\n\nLocal quality-gate findings (HARD):\n"
            + hard_text
            + "\n\nLocal quality-gate findings (SOFT):\n"
            + soft_text
            + history_block
            + "\n\nDraft:\n"
            + draft_markdown
        )
        raw = await self.client.chat_for_role(
            role="critic",
            system_prompt=BRIEFING_CRITIC_PROMPT,
            user_content=user_msg,
            json_response=True,
        )
        try:
            parsed = self.client.parse_json_response(raw)
            if not isinstance(parsed, dict):
                raise ValueError("critic payload must be an object")
            passed = bool(parsed.get("passed", False))
            score = max(0, min(100, int(parsed.get("score", 0))))
            dimension_scores = parsed.get("dimension_scores", {}) or {}
            if not isinstance(dimension_scores, dict):
                dimension_scores = {}
            dims = {
                "actionability": max(
                    0, min(100, int(dimension_scores.get("actionability", score)))
                ),
                "source_diversity": max(
                    0, min(100, int(dimension_scores.get("source_diversity", score)))
                ),
                "link_hygiene": max(
                    0, min(100, int(dimension_scores.get("link_hygiene", score)))
                ),
                "clarity": max(
                    0, min(100, int(dimension_scores.get("clarity", score)))
                ),
                "style": max(0, min(100, int(dimension_scores.get("style", score)))),
                "novelty": max(
                    0, min(100, int(dimension_scores.get("novelty", score)))
                ),
            }
            issues = parsed.get("issues", [])
            recommendations = parsed.get("recommendations", [])
            if not isinstance(issues, list):
                issues = [str(issues)]
            if not isinstance(recommendations, list):
                recommendations = [str(recommendations)]
            return {
                "passed": passed,
                "score": score,
                "dimension_scores": dims,
                "issues": [str(i) for i in issues[:12]],
                "recommendations": [str(r) for r in recommendations[:12]],
            }
        except Exception:
            logger.warning("Failed to parse briefing critique JSON, using fallback")
            return {
                "passed": False,
                "score": 0,
                "dimension_scores": {
                    "actionability": 0,
                    "source_diversity": 0,
                    "link_hygiene": 0,
                    "clarity": 0,
                    "style": 0,
                    "novelty": 0,
                },
                "issues": ["Critic response parsing failed"],
                "recommendations": [
                    "Regenerate briefing with stricter structure and actionable guidance"
                ],
            }
