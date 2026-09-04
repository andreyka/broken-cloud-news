"""Writer LLM interactions."""

import base64
import json
import logging
import re
import hashlib
from datetime import date
from typing import Any

from bcn.briefing.text import preferred_item_url
from bcn.briefing.text import sanitize_item_title
from bcn.services.tools import allow_tool_urls
from bcn.services.tools import fetch_page_content
from bcn.services.writer.prompt import BRIEFING_ENRICHER_PROMPT
from bcn.services.writer.prompt import BRIEFING_REWRITE_PROMPT
from bcn.services.writer.prompt import BRIEFING_STORY_CARD_PROMPT
from bcn.services.writer.prompt import BRIEFING_SYSTEM_PROMPT
from bcn.services.writer.prompt import BRIEFING_TIGHTENER_PROMPT
from bcn.services.writer.prompt import COVER_ART_SYSTEM_PROMPT
from bcn.services.writer.prompt import default_writer_prompts
from bcn.common.llm import LLMClient
from bcn.common.scraper import Scraper

logger = logging.getLogger(__name__)

_SKIP_DOMAINS = frozenset(
    {
        "nvd.nist.gov",
        "cve.mitre.org",
        "cve.org",
        "access.redhat.com",
    }
)

_IMAGE_MODEL_HINTS = frozenset(
    {
        "image",
        "imagen",
        "nanobanana",
        "nano-banana",
        "nano banana",
    }
)

_COVER_PROMPT_SUFFIX = (
    "editorial cover image, clean composition, coherent perspective, "
    "high-quality rendering, "
    "no text, no watermark, no logo, no horror, no creepy faces, no hooded hacker, "
    "no glowing eyes, no distorted anatomy, no extra fingers, no extra limbs, "
    "no floating UI clutter, no generic cyberpunk neon"
)

# Rotating art directions so covers do not converge on one look. Picked
# deterministically from the topics + date so same-day retries stay stable.
_COVER_ART_DIRECTIONS = (
    "gritty documentary photojournalism, handheld framing, muted colors, real-world wear and dust",
    "film-noir chiaroscuro, deep shadows, one hard light source, near-monochrome with a single accent color",
    "brutalist architecture wide shot, monumental concrete forms, one tiny human figure for scale, overcast sky",
    "extreme macro photography of physical hardware, shallow depth of field, visible dust and fingerprints",
    "aerial landscape at dusk, infrastructure as terrain, long shadows, cinematic scale",
    "analog collage and cut-paper illustration, tactile textures, bold flat colors",
    "technical blueprint aesthetic, fine white linework on deep blue, isometric schematic beauty",
    "stormy weather over physical infrastructure, wind and rain, dramatic sky, nature as the threat",
    "vintage 1970s science-magazine illustration, film grain, warm palette, retro-futurism gone slightly wrong",
    "minimalist still life, one symbolic object on a seamless backdrop, studio lighting, generous negative space",
)


class WriterLLM:
    def __init__(self, client: LLMClient, *, prompts: dict[str, str] | None = None):
        self.client = client
        self.scraper = Scraper()
        self.prompts = default_writer_prompts()
        for key, value in (prompts or {}).items():
            text = str(value or "").strip()
            if text:
                self.prompts[str(key)] = text

    async def close(self) -> None:
        """Release writer helper resources."""
        await self.scraper.close()

    async def generate_briefing(
        self,
        items: list[dict],
        recent_briefings: list[dict] | None = None,
        mode: str = "standard",
        min_chars: int | None = None,
        target_chars: int | None = None,
        hard_max_chars: int | None = None,
    ) -> str:
        """Generate an editorial briefing from story cards derived from items."""
        entries: list[str] = []
        for item in items:
            entries.append(await self._build_briefing_entry(item))

        story_cards = await self._build_story_cards_markdown(entries, items)
        cards_text = "\n\n".join(story_cards)
        style_memory = self._build_style_memory(recent_briefings or [])
        topic_memory = self._build_topic_memory(recent_briefings or [])
        if mode == "quiet_day":
            mode_block = (
                "Mode: quiet_day.\n"
                "- Fewer stories are acceptable.\n"
                "- Add deeper operator guidance and telemetry checks per item.\n"
                "- Explain tradeoffs clearly.\n"
            )
        elif mode == "monthly_newsletter":
            mode_block = (
                "Mode: monthly_newsletter.\n"
                "- Build a richer, longer editorial brief.\n"
                "- Prioritize trend synthesis across incidents and advisories.\n"
                "- Provide practical operator takeaways for each major section.\n"
            )
        elif mode == "weekly_flagship":
            mode_block = (
                "Mode: weekly_flagship (inbox-native edition for newsletter readers, "
                "NOT a Telegram post).\n"
                "- Open with a short intro paragraph stating the week's thesis.\n"
                "- One substantive section per story: 3-6 sentences with the "
                "exploit path, blast radius, and what to do this week.\n"
                "- Close with a takeaway paragraph that connects the week's stories.\n"
                "- Target 3000-5000 characters; longer is fine, thin is not.\n"
            )
        else:
            mode_block = "Mode: standard daily briefing."
        if mode != "weekly_flagship" and min_chars and hard_max_chars:
            mode_block += (
                f"\n- Length: {int(min_chars)}-{int(hard_max_chars)} characters "
                f"(target ~{int(target_chars or hard_max_chars)}). Every story card "
                "gets its own section with at least two substantive sentences; "
                "never stub or drop a card to fit.\n"
            )
        user_msg = (
            f"{mode_block}\n\nStory cards ({len(story_cards)} total). "
            "Use every `URL` exactly once in the final briefing.\n\n" + cards_text
        )
        if style_memory:
            user_msg += (
                "\n\nRecent briefing patterns to avoid repeating:\n" + style_memory
            )
        if topic_memory:
            user_msg += "\n\n" + topic_memory

        tool_urls = self._tool_allowlist_urls(items)
        tools = [fetch_page_content] if tool_urls else None
        with allow_tool_urls(tool_urls):
            return await self.client.chat_for_role(
                role="writer",
                system_prompt=self.prompts["briefing_system"],
                user_content=user_msg,
                tools=tools,
            )

    async def tighten_briefing(
        self,
        markdown: str,
        target_chars: int = 900,
        hard_max_chars: int = 1000,
    ) -> str:
        """Compress briefing text while preserving links and practical meaning."""
        user_msg = (
            f"Target length: <= {target_chars} chars (hard max {hard_max_chars}).\n\n"
            f"Digest:\n{markdown}"
        )
        tightened = await self.client.chat_for_role(
            role="writer",
            system_prompt=self.prompts["briefing_tightener"],
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

        if mode == "quiet_day":
            mode_hint = (
                "quiet_day: deepen practical guidance per item and include concrete playbook flavor."
            )
        elif mode == "monthly_newsletter":
            mode_hint = (
                "monthly_newsletter: synthesize broader trends with richer depth and stronger sectioned narrative."
            )
        elif mode == "weekly_flagship":
            mode_hint = (
                "weekly_flagship: inbox-native weekly edition; deepen each section "
                "to 3-6 sentences and keep the intro/closing thesis paragraphs."
            )
        else:
            mode_hint = "standard: balanced actionable digest."
        user_msg = (
            f"Length goal: {min_chars}-{hard_max_chars} chars (target ~{target_chars}).\n"
            f"Mode: {mode_hint}\n"
            f"Current draft length: {len(draft_markdown)} chars.\n\n"
            f"Current draft:\n{draft_markdown}\n\n"
            f"Story cards ({len(story_cards)} total):\n\n" + cards_text
        )
        if missing_urls:
            missing = "\n".join(f"- {u}" for u in missing_urls)
            user_msg += (
                "\n\nRequired URLs currently missing and must be included exactly once:\n"
                + missing
            )

        tool_urls = self._tool_allowlist_urls(items)
        tools = [fetch_page_content] if tool_urls else None
        with allow_tool_urls(tool_urls):
            rewritten = await self.client.chat_for_role(
                role="writer",
                system_prompt=self.prompts["briefing_enricher"],
                user_content=user_msg,
                tools=tools,
            )
        return rewritten.strip()

    async def revise_briefing(
        self,
        draft_markdown: str,
        items: list[dict],
        feedback: list[str],
        feedback_context: dict[str, Any] | None = None,
        recent_briefings: list[dict] | None = None,
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

        fb = (
            "\n".join(f"- {line}" for line in feedback[:40])
            or "- improve overall quality"
        )
        structured = feedback_context or {}
        structured_json = json.dumps(structured, ensure_ascii=False, indent=2)
        style_memory = self._build_style_memory(recent_briefings or [])
        topic_memory = self._build_topic_memory(recent_briefings or [])
        if mode == "quiet_day":
            mode_text = "quiet_day: deeper practical guidance with fewer items accepted"
        elif mode == "monthly_newsletter":
            mode_text = "monthly_newsletter: broader trend synthesis with deeper multi-story analysis"
        elif mode == "weekly_flagship":
            mode_text = (
                "weekly_flagship: inbox-native weekly edition with intro thesis, "
                "substantive per-story sections, and a closing takeaway"
            )
        else:
            mode_text = "standard daily briefing"
        user_msg = (
            f"Mode: {mode_text}\n"
            f"Target length: {min_chars}-{hard_max_chars} chars (ideal ~{target_chars}).\n\n"
            "Structured feedback context (JSON, blocking-first priority):\n"
            f"{structured_json}\n\n"
            "Feedback to address (flattened supplemental list):\n"
            f"{fb}\n\n"
            "Current draft:\n"
            f"{draft_markdown}\n\n"
            "Story cards:\n\n" + cards_text
        )
        if style_memory:
            user_msg += (
                "\n\nRecent briefing patterns to avoid repeating:\n" + style_memory
            )
        if topic_memory:
            user_msg += "\n\n" + topic_memory
        tool_urls = self._tool_allowlist_urls(items)
        tools = [fetch_page_content] if tool_urls else None
        with allow_tool_urls(tool_urls):
            revised = await self.client.chat_for_role(
                role="writer",
                system_prompt=self.prompts["briefing_rewrite"],
                user_content=user_msg,
                tools=tools,
            )
        return revised.strip()

    async def generate_briefing_title(self, markdown: str) -> str:
        """Generate a short headline for a finished briefing."""
        from bcn.briefing.text import derive_briefing_title
        from bcn.services.writer.prompt import BRIEFING_TITLE_PROMPT

        try:
            raw = await self.client.chat_for_role(
                role="writer",
                system_prompt=BRIEFING_TITLE_PROMPT,
                user_content=markdown[:6000],
                retries=1,
            )
        except Exception:
            logger.exception("Title generation failed; deriving from opener")
            return derive_briefing_title(markdown)
        title = (raw or "").strip().strip('"').strip("'").splitlines()[0].strip()
        title = title.rstrip(" .")
        if not title or len(title) > 90:
            return derive_briefing_title(markdown)
        return title

    async def generate_cover_prompt(self, topics: str) -> str:
        """Generate a Flux image prompt from today's security topics."""
        direction = self._pick_cover_art_direction(topics)
        user_msg = (
            f"Topics:\n{topics}\n\n"
            f"Today's art direction (follow it, do not default to a clean "
            f"modern office): {direction}"
        )
        raw = await self.client.chat_for_role(
            role="writer",
            system_prompt=self.prompts["cover_art_system"],
            user_content=user_msg,
        )
        return self._finalize_cover_prompt(raw)

    def supports_cover_image_generation(self) -> bool:
        """Return True when the cover role points to a supported image-capable model."""
        endpoint = self.client._endpoint("cover")
        model = (endpoint.model or "").lower()
        if endpoint.provider in {"gemini", "vertexai"}:
            return any(hint in model for hint in _IMAGE_MODEL_HINTS)
        if endpoint.provider == "openai_compat":
            return model.startswith("gpt-image") or model.startswith("chatgpt-image")
        return False

    async def generate_cover_image_data_url(self, prompt_text: str) -> str | None:
        """Generate cover image bytes via the configured image backend and return a data URL."""
        endpoint = self.client._endpoint("cover")
        if endpoint.provider in {"gemini", "vertexai"}:
            mime_type, image_bytes = await self.client.generate_gemini_image(
                endpoint, prompt_text
            )
        elif endpoint.provider == "openai_compat":
            mime_type, image_bytes = await self.client.generate_openai_image(
                endpoint, prompt_text
            )
        else:
            return None
        encoded = base64.b64encode(image_bytes).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    def prompt_versions(self) -> dict[str, dict[str, str | int]]:
        out: dict[str, dict[str, str | int]] = {}
        for name, prompt in self.prompts.items():
            digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            out[name] = {"sha256": digest, "chars": len(prompt)}
        return out

    @staticmethod
    def _pick_cover_art_direction(topics: str) -> str:
        digest = hashlib.sha256(
            f"{date.today().toordinal()}:{topics}".encode("utf-8")
        ).hexdigest()
        return _COVER_ART_DIRECTIONS[int(digest, 16) % len(_COVER_ART_DIRECTIONS)]

    @staticmethod
    def _finalize_cover_prompt(raw: str) -> str:
        """Normalize the model output into a stable editorial-looking cover prompt."""
        cleaned = raw.replace("`", " ").replace("\n", " ").strip(" ,")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if not cleaned:
            cleaned = "cloud security incident scene"

        lower = cleaned.lower()
        if _COVER_PROMPT_SUFFIX.lower() in lower:
            return cleaned
        return f"{cleaned}, {_COVER_PROMPT_SUFFIX}"

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

            # Heading lines only: the house style also bolds inline key terms,
            # which are vocabulary, not covered topics.
            topics = re.findall(r"^\*\*([^*\n]+?)\*\*\s*$", body, flags=re.MULTILINE)
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
            parts.append(
                "Previously used URLs (DO NOT reuse):\n"
                + "\n".join(f"- {u}" for u in seen_urls)
            )
        if seen_topics:
            parts.append(
                "Previously covered topics (DO NOT repeat):\n"
                + "\n".join(f"- {t}" for t in seen_topics[:30])
            )
        if seen_cves:
            parts.append(
                "Previously mentioned CVEs:\n" + "\n".join(f"- {c}" for c in seen_cves)
            )

        return "\n\n".join(parts)

    async def _build_story_cards_markdown(
        self, entries: list[str], items: list[dict]
    ) -> list[str]:
        """Generate unstructured markdown story cards before prose generation."""
        user_msg = f"Candidate items ({len(entries)} total):\n\n" + "\n\n".join(entries)
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
            summary = str(item.get("summary") or item.get("title") or "").strip()
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
        main_url = preferred_item_url(item) or str(item.get("url") or "").strip()
        ref_urls = self._extract_reference_urls(item.get("raw_data"), main_url)
        ref_urls = await self._filter_live_reference_urls(ref_urls)
        title = sanitize_item_title(str(item.get("title") or ""))

        entry = (
            f"- Title: {title}\n"
            f"  URL: {main_url}\n"
            f"  Summary: {item.get('summary', '')}\n"
            f"  Score: {item.get('relevance_score', 0)}/10\n"
            f"  Source: {item.get('source_type', '')}"
        )
        if ref_urls:
            entry += "\n  Reference links:\n" + "\n".join(
                f"    - {u}" for u in ref_urls[:3]
            )
        else:
            entry += "\n  Reference links: none"
        return entry

    def _extract_reference_urls(self, raw_data: Any, main_url: str) -> list[str]:
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
            if (
                not url
                or url in seen
                or url == main_url
                or any(domain in url for domain in _SKIP_DOMAINS)
            ):
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
        return await self.scraper.is_url_live(
            url,
            cache=self.client._url_status_cache,
            timeout_ms=10000,
        )

    @staticmethod
    def _strip_cover_image(markdown: str) -> str:
        """Remove leading markdown image syntax from briefing body text."""
        return re.sub(r"^!\[[^\]]*]\([^)]+\)\s*\n*", "", markdown.strip())
