"""Writer LLM interactions."""

import base64
import json
import logging
import re
from typing import Any

from bcn.agents.tools import allow_tool_urls
from bcn.agents.tools import fetch_page_content
from bcn.agents.writer.prompt import BRIEFING_ENRICHER_PROMPT
from bcn.agents.writer.prompt import BRIEFING_REWRITE_PROMPT
from bcn.agents.writer.prompt import BRIEFING_STORY_CARD_PROMPT
from bcn.agents.writer.prompt import BRIEFING_SYSTEM_PROMPT
from bcn.agents.writer.prompt import BRIEFING_TIGHTENER_PROMPT
from bcn.agents.writer.prompt import COVER_ART_SYSTEM_PROMPT
from bcn.common.llm import LLMClient

logger = logging.getLogger(__name__)

_SKIP_DOMAINS = frozenset({
    "nvd.nist.gov",
    "cve.mitre.org",
    "cve.org",
    "access.redhat.com",
})

from bcn.common.scraper import Scraper


class WriterLLM:

    def __init__(self, client: LLMClient):
        self.client = client
        self.scraper = Scraper()

    async def generate_briefing(
        self,
        items: list[dict],
        recent_briefings: list[dict] | None = None,
        mode: str = "standard",
    ) -> str:
        """Generate an editorial briefing from story cards derived from items."""
        entries: list[str] = []
        for item in items:
            entries.append(await self._build_briefing_entry(item))

        story_cards = await self._build_story_cards_markdown(entries, items)
        cards_text = "\n\n".join(story_cards)
        style_memory = self._build_style_memory(recent_briefings or [])
        topic_memory = self._build_topic_memory(recent_briefings or [])
        mode_block = (
            "Mode: quiet_day.\n"
            "- Fewer stories are acceptable.\n"
            "- Add deeper operator guidance and telemetry checks per item.\n"
            "- Explain tradeoffs clearly.\n"
            if mode == "quiet_day" else "Mode: standard daily briefing.")
        user_msg = (f"{mode_block}\n\nStory cards ({len(story_cards)} total). "
                    "Use every `URL` exactly once in the final briefing.\n\n" +
                    cards_text)
        if style_memory:
            user_msg += "\n\nRecent briefing patterns to avoid repeating:\n" + style_memory
        if topic_memory:
            user_msg += "\n\n" + topic_memory

        tool_urls = self._tool_allowlist_urls(items)
        tools = [fetch_page_content] if tool_urls else None
        with allow_tool_urls(tool_urls):
            return await self.client.chat_for_role(
                role="writer",
                system_prompt=BRIEFING_SYSTEM_PROMPT,
                user_content=user_msg,
                tools=tools)

    async def tighten_briefing(
        self,
        markdown: str,
        target_chars: int = 900,
        hard_max_chars: int = 1000,
    ) -> str:
        """Compress briefing text while preserving links and practical meaning."""
        user_msg = (
            f"Target length: <= {target_chars} chars (hard max {hard_max_chars}).\n\n"
            f"Digest:\n{markdown}")
        tightened = await self.client.chat_for_role(
            role="writer",
            system_prompt=BRIEFING_TIGHTENER_PROMPT,
            user_content=user_msg,
        )
        return tightened.strip()

    async def enrich_briefing(
        self,
        draft_markdown: str,
        items: list[dict],
        *,
        min_chars: int = 1200,
        target_chars: int = 1700,
        hard_max_chars: int = 2300,
        missing_urls: list[str] | None = None,
        mode: str = "standard",
    ) -> str:
        """Rewrite a draft briefing to improve depth and ensure URL coverage."""
        entries: list[str] = []
        for item in items:
            entries.append(await self._build_briefing_entry(item))
        story_cards = await self._build_story_cards_markdown(entries, items)
        cards_text = "\n\n".join(story_cards)

        mode_hint = (
            "quiet_day: deepen practical guidance per item and include concrete playbook flavor."
            if mode == "quiet_day" else "standard: balanced actionable digest.")
        user_msg = (
            f"Length goal: {min_chars}-{hard_max_chars} chars (target ~{target_chars}).\n"
            f"Mode: {mode_hint}\n"
            f"Current draft length: {len(draft_markdown)} chars.\n\n"
            f"Current draft:\n{draft_markdown}\n\n"
            f"Story cards ({len(story_cards)} total):\n\n" + cards_text)
        if missing_urls:
            missing = "\n".join(f"- {u}" for u in missing_urls)
            user_msg += "\n\nRequired URLs currently missing and must be included exactly once:\n" + missing

        tool_urls = self._tool_allowlist_urls(items)
        tools = [fetch_page_content] if tool_urls else None
        with allow_tool_urls(tool_urls):
            rewritten = await self.client.chat_for_role(
                role="writer",
                system_prompt=BRIEFING_ENRICHER_PROMPT,
                user_content=user_msg,
                tools=tools)
        return rewritten.strip()

    async def revise_briefing(
        self,
        draft_markdown: str,
        items: list[dict],
        feedback: list[str],
        feedback_context: dict[str, Any] | None = None,
        *,
        mode: str = "standard",
        min_chars: int = 1200,
        target_chars: int = 1700,
        hard_max_chars: int = 2300,
    ) -> str:
        """Regenerate a briefing draft using explicit critic/gate feedback."""
        entries: list[str] = []
        for item in items:
            entries.append(await self._build_briefing_entry(item))
        story_cards = await self._build_story_cards_markdown(entries, items)
        cards_text = "\n\n".join(story_cards)

        fb = "\n".join(f"- {line}"
                       for line in feedback[:40]) or "- improve overall quality"
        structured = feedback_context or {}
        structured_json = json.dumps(structured, ensure_ascii=False, indent=2)
        mode_text = (
            "quiet_day: deeper practical guidance with fewer items accepted"
            if mode == "quiet_day" else "standard daily briefing")
        user_msg = (
            f"Mode: {mode_text}\n"
            f"Target length: {min_chars}-{hard_max_chars} chars (ideal ~{target_chars}).\n\n"
            "Structured feedback context (JSON, blocking-first priority):\n"
            f"{structured_json}\n\n"
            "Feedback to address (flattened supplemental list):\n"
            f"{fb}\n\n"
            "Current draft:\n"
            f"{draft_markdown}\n\n"
            "Story cards:\n\n" + cards_text)
        tool_urls = self._tool_allowlist_urls(items)
        tools = [fetch_page_content] if tool_urls else None
        with allow_tool_urls(tool_urls):
            revised = await self.client.chat_for_role(
                role="writer",
                system_prompt=BRIEFING_REWRITE_PROMPT,
                user_content=user_msg,
                tools=tools)
        return revised.strip()

    async def generate_cover_prompt(self, topics: str) -> str:
        """Generate a Flux image prompt from today's security topics."""
        user_msg = f"Topics:\n{topics}"
        raw = await self.client.chat_for_role(
            role="writer",
            system_prompt=COVER_ART_SYSTEM_PROMPT,
            user_content=user_msg,
        )
        return raw.replace("`", "").strip()

    def supports_cover_image_generation(self) -> bool:
        """Return True when the cover role points to a Gemini image model."""
        endpoint = self.client._endpoint("cover")
        model = (endpoint.model or "").lower()
        return endpoint.provider in {"gemini", "vertexai"} and "image" in model

    async def generate_cover_image_data_url(self,
                                            prompt_text: str) -> str | None:
        """Generate cover image bytes via Gemini and return a data URL."""
        endpoint = self.client._endpoint("cover")
        if endpoint.provider not in {"gemini", "vertexai"}:
            return None
        mime_type, image_bytes = await self.client.generate_gemini_image(
            endpoint, prompt_text)
        encoded = base64.b64encode(image_bytes).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    def prompt_versions(self) -> dict[str, dict[str, str | int]]:
        import hashlib
        out: dict[str, dict[str, str | int]] = {}
        prompts = {
            "briefing_system": BRIEFING_SYSTEM_PROMPT,
            "briefing_story_card": BRIEFING_STORY_CARD_PROMPT,
            "briefing_tightener": BRIEFING_TIGHTENER_PROMPT,
            "briefing_enricher": BRIEFING_ENRICHER_PROMPT,
            "briefing_rewrite": BRIEFING_REWRITE_PROMPT,
            "cover_art_system": COVER_ART_SYSTEM_PROMPT,
        }
        for name, prompt in prompts.items():
            digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            out[name] = {"sha256": digest, "chars": len(prompt)}
        return out

    def _build_style_memory(self, recent_briefings: list[dict]) -> str:
        """Build compact style-memory snippets to reduce repetitive writing."""
        snippets: list[str] = []
        for idx, briefing in enumerate(recent_briefings[:8], start=1):
            body = self._strip_cover_image(briefing.get("content_markdown", ""))
            lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
            if not lines:
                continue

            headings = re.findall(r"^\*\*(.+?)\*\*$", body, flags=re.MULTILINE)
            opening = lines[0][:140]
            closing = lines[-1][:140]
            snippets.append(
                f"{idx}) opening='{opening}' | headings={headings[:4]} | closing='{closing}'"
            )

        return "\n".join(snippets)

    def _build_topic_memory(self, recent_briefings: list[dict]) -> str:
        """Build a list of previously covered topics, URLs, and CVEs.

        This gives the writer explicit visibility into what was already
        published so it can avoid repeating the same content.
        """
        all_urls: list[str] = []
        all_topics: list[str] = []
        all_cves: list[str] = []

        for briefing in recent_briefings[:10]:
            body = briefing.get("content_markdown", "") or ""

            # Extract markdown links
            urls = re.findall(r"\[.*?\]\((https?://[^\)]+)\)", body)
            all_urls.extend(urls)

            # Extract bold topic headings (e.g. **Title Here**)
            topics = re.findall(r"\*\*(.+?)\*\*", body)
            all_topics.extend(topics)

            # Extract CVE IDs
            cves = re.findall(r"CVE-\d{4}-\d+", body, flags=re.IGNORECASE)
            all_cves.extend(cves)

        # Deduplicate
        seen_urls = list(dict.fromkeys(all_urls))
        seen_topics = list(dict.fromkeys(all_topics))
        seen_cves = list(dict.fromkeys(all_cves))

        if not seen_urls and not seen_topics and not seen_cves:
            return ""

        parts: list[str] = []
        if seen_urls:
            parts.append("Previously used URLs (DO NOT reuse):\n" +
                         "\n".join(f"- {u}" for u in seen_urls))
        if seen_topics:
            parts.append("Previously covered topics (DO NOT repeat):\n" +
                         "\n".join(f"- {t}" for t in seen_topics[:30]))
        if seen_cves:
            parts.append("Previously mentioned CVEs:\n" +
                         "\n".join(f"- {c}" for c in seen_cves))

        return "\n\n".join(parts)

    async def _build_story_cards_markdown(self, entries: list[str],
                                          items: list[dict]) -> list[str]:
        """Generate unstructured markdown story cards before prose generation."""
        user_msg = (f"Candidate items ({len(entries)} total):\n\n" +
                    "\n\n".join(entries))
        raw = await self.client.chat_for_role(
            role="writer",
            system_prompt=BRIEFING_STORY_CARD_PROMPT,
            user_content=user_msg,
            json_response=False,
        )
        blocks = [
            b.strip()
            for b in re.split(r"^---\s*$", raw, flags=re.MULTILINE)
            if b.strip()
        ]
        if blocks:
            return blocks

        logger.warning(
            "Failed to parse markdown story cards; using deterministic fallback cards"
        )
        out: list[str] = []
        for item in items:
            url = str(item.get("url") or "").strip()
            summary = str(item.get("summary") or item.get("title") or
                          "").strip()
            if not url:
                continue
            card = (
                f"URL: {url}\n"
                f"What Happened: {summary}\n"
                "Why Now: Emerging cloud-security signal with near-term operational impact.\n"
                "Who is Impacted: Cloud operators and teams running production workloads.\n"
                "Offensive Angle: Adversaries can abuse the weakness to gain access or evade controls.\n"
                "Defensive Action: Patch exposed systems and enable targeted detection immediately.\n"
            )
            out.append(card)
        return out

    async def _build_briefing_entry(self, item: dict[str, Any]) -> str:
        """Build a single item block for briefing generation."""
        main_url = str(item.get("url") or "").strip()
        ref_urls = self._extract_reference_urls(item.get("raw_data"), main_url)
        ref_urls = await self._filter_live_reference_urls(ref_urls)

        entry = (f"- Title: {item.get('title', '')}\n"
                 f"  URL: {main_url}\n"
                 f"  Summary: {item.get('summary', '')}\n"
                 f"  Score: {item.get('relevance_score', 0)}/10\n"
                 f"  Source: {item.get('source_type', '')}")
        if ref_urls:
            entry += "\n  Reference links:\n" + "\n".join(
                f"    - {u}" for u in ref_urls[:3])
        else:
            entry += "\n  Reference links: none"
        return entry

    def _extract_reference_urls(self, raw_data: Any,
                                main_url: str) -> list[str]:
        """Extract and de-duplicate candidate reference URLs from raw source payload."""
        raw: dict[str, Any] = {}
        if isinstance(raw_data, str):
            try:
                raw = json.loads(raw_data)
            except (json.JSONDecodeError, TypeError):
                raw = {}
        elif isinstance(raw_data, dict):
            raw = raw_data

        refs = raw.get("references", [])
        seen: set[str] = set()
        out: list[str] = []

        for ref in refs:
            url = ref.get("url", "") if isinstance(ref, dict) else str(ref)
            url = url.strip()
            if (not url or url in seen or url == main_url or
                    any(domain in url for domain in _SKIP_DOMAINS)):
                continue
            seen.add(url)
            out.append(url)

        return out

    def _tool_allowlist_urls(self, items: list[dict]) -> list[str]:
        """Build URL allowlist for LLM fetch tool from selected item context."""
        seen: set[str] = set()
        out: list[str] = []
        for item in items:
            main_url = str(item.get("url") or "").strip()
            if main_url and main_url not in seen:
                seen.add(main_url)
                out.append(main_url)

            refs = self._extract_reference_urls(item.get("raw_data"), main_url)
            for ref in refs:
                if ref not in seen:
                    seen.add(ref)
                    out.append(ref)
        return out

    async def _filter_live_reference_urls(self, urls: list[str]) -> list[str]:
        """Drop broken links for domains where stale references are common."""
        kept: list[str] = []
        for url in urls:
            if await self._is_reference_url_live(url):
                kept.append(url)
            else:
                logger.info("Dropping dead reference URL: %s", url)
        return kept

    async def _is_reference_url_live(self, url: str) -> bool:
        """Check URL liveness for GitHub links to avoid obvious 404 references."""
        if not url.startswith(("http://", "https://")):
            return False
        if "github.com" not in url:
            return True
        if url in self.client._url_status_cache:
            return self.client._url_status_cache[url]

        try:
            status, _ = await self.scraper.fetch_text(url,
                                                      method="HEAD",
                                                      timeout_ms=10000)
            alive = status in {200, 401, 403, 405, 429}
        except Exception:
            alive = False

        self.client._url_status_cache[url] = alive
        return alive

    @staticmethod
    def _strip_cover_image(markdown: str) -> str:
        """Remove leading markdown image syntax from briefing body text."""
        return re.sub(r"^!\[[^\]]*]\([^)]+\)\s*\n*", "", markdown.strip())
