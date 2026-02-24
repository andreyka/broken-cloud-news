"""Factual verification checks for briefing drafts."""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from bcn.briefing.text import normalize_url
from bcn.config import Settings
from bcn.llm import LLMClient

logger = logging.getLogger(__name__)

_CTF_EVENT_PATTERN = re.compile(
    r"\b(ctf|capture[-\s]the[-\s]flag|challenge|webinar|conference|meetup|call for papers)\b",
    re.IGNORECASE,
)


class BriefingFactVerifier:
    """Runs factual checks (deterministic + LLM) before publishing."""

    def __init__(self, settings: Settings, llm: LLMClient | None = None) -> None:
        self.settings = settings
        self.llm = llm or LLMClient(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            timeout=settings.llm_timeout,
        )
        self._http = httpx.AsyncClient(timeout=12)
        self._url_liveness_cache: dict[str, bool] = {}

    async def close(self) -> None:
        await self._http.aclose()

    async def evaluate(
        self,
        markdown: str,
        items: list[dict[str, Any]],
        *,
        mode: str = "standard",
    ) -> dict[str, Any]:
        """Evaluate factual quality and return a blocking/non-blocking report."""
        body = (markdown or "").strip()
        deterministic_hard_issues: list[str] = []
        soft_issues: list[str] = []
        recommendations: list[str] = []

        all_urls = self._extract_urls_in_order(body)
        dead_urls = await self._find_dead_urls(all_urls)
        if dead_urls:
            deterministic_hard_issues.append(
                "Dead or unreachable links detected: " + ", ".join(dead_urls[:3])
            )
            recommendations.append("Replace or remove dead links before publishing.")

        top_story_is_ctf_or_event = self._top_story_is_ctf_or_event(body, items)
        if top_story_is_ctf_or_event:
            deterministic_hard_issues.append("Top story appears to be CTF/event-style announcement.")
            recommendations.append(
                "Promote a production-impact cloud-security story above event/CTF items."
            )

        llm_report = await self.llm.verify_briefing_facts(
            draft_markdown=body,
            items=items,
            mode=mode,
            deterministic_issues=deterministic_hard_issues,
        )

        llm_hard_issues = [str(i) for i in llm_report.get("hard_issues", [])]
        hard_issues = list(deterministic_hard_issues)
        hard_issues.extend(llm_hard_issues)
        soft_issues.extend([str(i) for i in llm_report.get("soft_issues", [])])
        recommendations.extend([str(i) for i in llm_report.get("recommendations", [])])

        # Deduplicate while preserving order.
        deterministic_hard_issues = list(dict.fromkeys([i for i in deterministic_hard_issues if i]))[:16]
        llm_hard_issues = list(dict.fromkeys([i for i in llm_hard_issues if i]))[:16]
        hard_issues = list(dict.fromkeys([i for i in hard_issues if i]))[:16]
        soft_issues = list(dict.fromkeys([i for i in soft_issues if i]))[:16]
        recommendations = list(dict.fromkeys([i for i in recommendations if i]))[:16]

        llm_passed = bool(llm_report.get("passed", False))
        llm_score = int(llm_report.get("score", 0) or 0)
        hard_penalty = min(35, len(hard_issues) * 12)
        score = max(0, llm_score - hard_penalty)
        # Block only on deterministic hard failures; LLM hard findings are advisory.
        passed = not deterministic_hard_issues

        return {
            "passed": passed,
            "score": score,
            "hard_issues": hard_issues,
            "blocking_hard_issues": deterministic_hard_issues,
            "llm_hard_issues": llm_hard_issues,
            "llm_passed": llm_passed,
            "soft_issues": soft_issues,
            "issues": (hard_issues + soft_issues)[:24],
            "recommendations": recommendations,
            "dead_urls": dead_urls[:12],
            "top_story_ok": not top_story_is_ctf_or_event,
        }

    async def _find_dead_urls(self, urls: list[str]) -> list[str]:
        max_links = max(1, int(self.settings.briefing_verifier_max_links))
        dead: list[str] = []
        for raw_url in urls[:max_links]:
            url = normalize_url(raw_url)
            if not url:
                continue
            alive = await self._is_url_live(url)
            if not alive:
                dead.append(url)
        return dead

    async def _is_url_live(self, url: str) -> bool:
        if not url.startswith(("http://", "https://")):
            return False
        if url in self._url_liveness_cache:
            return self._url_liveness_cache[url]

        alive_status = {200, 401, 403, 405, 429}
        alive = False
        try:
            resp = await self._http.head(url, follow_redirects=True)
            alive = resp.status_code in alive_status
            if not alive and resp.status_code >= 500:
                # Some sources reject HEAD; retry with GET.
                resp = await self._http.get(url, follow_redirects=True)
                alive = resp.status_code in alive_status
        except Exception:
            alive = False

        self._url_liveness_cache[url] = alive
        return alive

    @staticmethod
    def _extract_urls_in_order(markdown: str) -> list[str]:
        raw = re.findall(r"https?://[^\s)\]>]+", markdown or "")
        return list(dict.fromkeys(raw))

    @staticmethod
    def _top_story_is_ctf_or_event(markdown: str, items: list[dict[str, Any]]) -> bool:
        body = (markdown or "").strip()
        link_match = re.search(r"\[([^\]]+)]\((https?://[^)]+)\)", body)
        if not link_match:
            return False

        top_label = (link_match.group(1) or "").strip()
        top_url = normalize_url(link_match.group(2))
        top_title = top_label
        by_url = {
            normalize_url(str(item.get("url", ""))): str(item.get("title") or "")
            for item in items
        }
        if top_url in by_url and by_url[top_url]:
            top_title = by_url[top_url]

        return bool(_CTF_EVENT_PATTERN.search(top_title))
