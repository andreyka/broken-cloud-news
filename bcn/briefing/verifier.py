"""Factual verification checks for briefing drafts."""

from __future__ import annotations

import logging
import re
from typing import Any

from bcn.agents.verifier.llm import VerifierLLM
from bcn.briefing.text import canonical_url_key
from bcn.briefing.text import extract_urls_in_order as extract_urls_in_order_from_text
from bcn.briefing.text import normalize_url
from bcn.common.config import Settings
from bcn.common.llm import LLMClient
from bcn.common.scraper import Scraper

logger = logging.getLogger(__name__)

_CTF_EVENT_PATTERN = re.compile(
    r"\b(ctf|capture[-\s]the[-\s]flag|challenge|webinar|conference|meetup|call for papers)\b",
    re.IGNORECASE,
)
_GHSA_ID_PATTERN = re.compile(
    r"\bGHSA-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}\b", re.IGNORECASE
)
_GITHUB_ADVISORY_URL_PATTERN = re.compile(
    r"https?://github\.com/advisories/(GHSA-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4})",
    re.IGNORECASE,
)


class BriefingFactVerifier:
    """Runs factual checks (deterministic + LLM) before publishing."""

    def __init__(self, settings: Settings, llm_client: LLMClient | None = None) -> None:
        self.settings = settings
        base_client = llm_client or LLMClient.from_settings(settings)
        self._owned_llm_client = llm_client is None
        self._llm_client = base_client
        self.verifier_llm = VerifierLLM(base_client)
        self.scraper = Scraper()
        self._url_liveness_cache: dict[str, bool] = {}

    async def close(self) -> None:
        await self.scraper.close()
        if self._owned_llm_client:
            await self._llm_client.close()

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

        unexpected_urls = self._find_unselected_url_mentions(body, items)
        if unexpected_urls:
            deterministic_hard_issues.append(
                "Briefing links URLs not present in selected items: "
                + ", ".join(unexpected_urls[:4])
            )
            recommendations.append(
                "Remove URLs that are not part of the selected items."
            )

        top_story_is_ctf_or_event = self._top_story_is_ctf_or_event(body, items)
        if top_story_is_ctf_or_event:
            deterministic_hard_issues.append(
                "Top story appears to be CTF/event-style announcement."
            )
            recommendations.append(
                "Promote a production-impact cloud-security story above event/CTF items."
            )
        unselected_mentions = self._find_unselected_advisory_mentions(body, items)
        if unselected_mentions:
            deterministic_hard_issues.append(
                "Briefing mentions advisory identifiers not present in selected items: "
                + ", ".join(unselected_mentions[:4])
            )
            recommendations.append(
                "Remove references to advisories not present in selected items."
            )

        llm_report = await self.verifier_llm.verify_briefing_facts(
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
        deterministic_hard_issues = list(
            dict.fromkeys([i for i in deterministic_hard_issues if i])
        )[:16]
        llm_hard_issues = list(dict.fromkeys([i for i in llm_hard_issues if i]))[:16]
        hard_issues = list(dict.fromkeys([i for i in hard_issues if i]))[:16]
        soft_issues = list(dict.fromkeys([i for i in soft_issues if i]))[:16]
        recommendations = list(dict.fromkeys([i for i in recommendations if i]))[:16]

        llm_passed = bool(llm_report.get("passed", False))
        llm_score = int(llm_report.get("score", 0) or 0)
        hard_penalty = min(35, len(hard_issues) * 12)
        score = max(0, llm_score - hard_penalty)
        llm_hard_blocking = bool(
            self.settings.briefing_verifier_block_on_llm_hard
        ) and bool(llm_hard_issues)
        passed = not deterministic_hard_issues and not llm_hard_blocking

        return {
            "passed": passed,
            "score": score,
            "hard_issues": hard_issues,
            "blocking_hard_issues": deterministic_hard_issues,
            "llm_hard_issues": llm_hard_issues,
            "llm_hard_blocking": llm_hard_blocking,
            "llm_passed": llm_passed,
            "soft_issues": soft_issues,
            "issues": (hard_issues + soft_issues)[:24],
            "recommendations": recommendations,
            "dead_urls": dead_urls[:12],
            "top_story_ok": not top_story_is_ctf_or_event,
        }

    def _find_unselected_advisory_mentions(
        self,
        markdown: str,
        items: list[dict[str, Any]],
    ) -> list[str]:
        selected_urls = {
            canonical_url_key(str(item.get("url", "")))
            for item in items
            if str(item.get("url", "")).strip()
        }
        selected_ghsas = self._collect_selected_ghsa_ids(items)
        mentions: list[str] = []
        seen: set[str] = set()

        for match in _GHSA_ID_PATTERN.finditer(markdown or ""):
            ghsa = match.group(0).upper()
            key = f"id:{ghsa.lower()}"
            if ghsa.lower() in selected_ghsas or key in seen:
                continue
            seen.add(key)
            mentions.append(ghsa)

        for match in _GITHUB_ADVISORY_URL_PATTERN.finditer(markdown or ""):
            url = canonical_url_key(match.group(0))
            ghsa = str(match.group(1) or "").upper()
            key = f"url:{url.lower()}"
            if url in selected_urls or ghsa.lower() in selected_ghsas or key in seen:
                continue
            seen.add(key)
            mentions.append(url)

        return mentions[:8]

    def _find_unselected_url_mentions(
        self,
        markdown: str,
        items: list[dict[str, Any]],
    ) -> list[str]:
        selected_urls = {
            canonical_url_key(str(item.get("url", "")))
            for item in items
            if str(item.get("url", "")).strip()
        }
        mentions: list[str] = []
        seen: set[str] = set()

        for raw_url in self._extract_urls_in_order(markdown):
            key = canonical_url_key(raw_url)
            if not key or key in selected_urls or key in seen:
                continue
            seen.add(key)
            mentions.append(key)

        return mentions[:8]

    @staticmethod
    def _collect_selected_ghsa_ids(items: list[dict[str, Any]]) -> set[str]:
        selected: set[str] = set()
        for item in items:
            text = " ".join(
                [
                    str(item.get("title") or ""),
                    str(item.get("summary") or ""),
                    str(item.get("url") or ""),
                ]
            )
            for match in _GHSA_ID_PATTERN.finditer(text):
                selected.add(match.group(0).lower())
        return selected

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
        return await self.scraper.is_url_live(
            url,
            cache=self._url_liveness_cache,
            timeout_ms=max(
                1000,
                int(self.settings.briefing_verifier_url_liveness_timeout_ms),
            ),
        )

    @staticmethod
    def _extract_urls_in_order(markdown: str) -> list[str]:
        return extract_urls_in_order_from_text(markdown or "")

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
