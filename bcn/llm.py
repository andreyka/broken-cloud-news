"""Qwen LLM client for item analysis and briefing generation."""

from __future__ import annotations

import asyncio
import json
import logging
import re

import httpx

from bcn.models import AnalysisResult

logger = logging.getLogger(__name__)

# Analyzer system prompt
ANALYZER_SYSTEM_PROMPT = (
    "You are a Senior Cloud Security Engineer curating the 'Broken Cloud' daily security brief. "
    "Your goal is to identify practical, hands-on cloud and cloud-native security content while "
    "filtering out marketing fluff, theoretical chatter, and off-topic items.\n\n"
    "SCOPE: The newsletter is strictly about CLOUD and CLOUD-NATIVE security — "
    "Kubernetes, Docker, containers, serverless, AWS/Azure/GCP, Terraform, IAM, CI/CD pipelines, "
    "supply chain security, cloud-native tooling. "
    "General AI/ML news, ICS/SCADA, on-prem-only vulnerabilities, and generic infosec commentary "
    "that don't directly relate to cloud infrastructure should score LOW (1-3).\n\n"
    "Analyze the following content and return JSON with:\n"
    "1. summary: A concise, technical summary (1-2 sentences MAX) focusing on the 'so what?' for a cloud practitioner. Be brief.\n"
    "2. relevance_score: 1-10 based on THESE criteria (not just severity!):\n"
    "   - 9-10: Active exploitation in the wild, public PoC available, or critical vuln widely "
    "     present in cloud workloads (Kubernetes, Docker, AWS/Azure/GCP, Terraform, IAM). "
    "     Must have concrete technical details, not just a CVE number.\n"
    "   - 7-8: Critical/High severity WITH detailed write-up, exploit path described, "
    "     or significant tooling for offensive/defensive cloud security. Affects common cloud components.\n"
    "   - 5-6: Important cloud advisory but lacking detail, or good cloud security content that's not immediately actionable.\n"
    "   - 3-4: Niche/ICS-only vulnerabilities, compliance updates, vendor-specific without broad cloud impact, "
    "     or general AI/security content only tangentially related to cloud.\n"
    "   - 1-2: Marketing fluff, policy-only content, or unrelated to cloud security practice.\n"
    "   KEY: A CRITICAL severity CVE with only a title and no details/PoC/exploit path should score 5-6, "
    "   NOT 8-10. Score is about actionability and depth, not just severity label.\n"
    "3. tags: Array of 3-5 technical tags.\n"
    "4. image_prompt: A creative, eye-catching image prompt for an AI art generator. "
    "Abstractly represent the core technical concept (e.g., cyberpunk server room, glowing digital shield). "
    "Avoid text. Make it dramatic and high contrast."
)

# Cover art system prompt
COVER_ART_SYSTEM_PROMPT = (
    "You are an AI art director for a security newsletter. Create a single, high-contrast, dramatic "
    "image prompt that abstractly represents the following security topics. Avoid text. Focus on visual "
    "metaphors (e.g., cloud, cybersecurity, data centers, digital storms, cyber shields, cyber warfare, "
    "cyberpunk). Be creative. Return ONLY the prompt text."
)


BRIEFING_SYSTEM_PROMPT = (
    "You are the editor-in-chief of 'Broken Cloud Daily Briefing', a punchy cloud-security newsletter "
    "beloved for its sharp wit, clear insight, and no-BS tone.\n\n"
    "SCOPE: This newsletter is strictly about CLOUD and CLOUD-NATIVE security. "
    "Only include items that directly relate to cloud infrastructure, containers, Kubernetes, "
    "serverless, AWS/Azure/GCP, Terraform, IAM, CI/CD, or supply chain security. "
    "Skip items that are general AI news, generic infosec commentary, or off-topic — even if they "
    "appear in the input. It is perfectly fine to have a briefing with only 1-2 items, or even to "
    "report that it's a quiet day.\n\n"
    "You will receive a set of scored security items. Each item has a title, summary, main URL, "
    "and possibly extra reference links (GitHub commits, blog write-ups, etc.).\n\n"
    "Write a single cohesive briefing in Markdown that:\n"
    "1. Opens with a short editorial intro (1 sentence) capturing the security mood of the day.\n"
    "2. Groups items by theme with a short, creative section name derived from the actual content. "
    "Each briefing should have unique section names — never reuse the same ones across days. "
    "Keep them to 2-3 words.\n"
    "3. For each item write ONE sentence — your sharp take on why it matters. "
    "Include the main title as a Markdown hyperlink using the item's URL. "
    "If the item includes 'Reference links', you may pick 1-2 of the MOST useful ones "
    "(prioritize: blog write-ups > GitHub PRs/commits > official advisories). "
    "Add them as extra inline links like [write-up](url) or [fix](url). "
    "Skip generic NVD/MITRE/errata links.\n"
    "CRITICAL: Only use URLs that are explicitly provided in the input data. "
    "NEVER fabricate, guess, or construct URLs. NEVER duplicate the main URL as a reference link. "
    "If an item has no reference links, just use the main URL — do NOT add extra links.\n"
    "4. Closes with a punchy one-liner sign-off.\n\n"
    "LENGTH GUIDANCE:\n"
    "The briefing is sent as a Telegram photo caption (limit: 1024 characters). "
    "You MUST aim for under 1000 characters. This is a hard target, not a suggestion. "
    "A single clean message with the cover image looks far better than a message with overflow. "
    "Brevity is the entire point: cut filler words, use short section names, one short sentence per item, "
    "skip the intro if space is tight.\n"
    "Going over 1000 characters should be RARE — only for truly exceptional days (major breach, "
    "critical 0-day with PoC). Even then, cap at 1500 characters.\n\n"
    "Do NOT include a title/header line — that will be added by the system. "
    "Do NOT use score numbers or 'Source:' labels. Write for humans, not robots."
)

_SKIP_DOMAINS = frozenset({
    "nvd.nist.gov", "cve.mitre.org", "cve.org", "access.redhat.com",
})


class LLMClient:
    """OpenAI-compatible chat client for the Qwen LLM on DGX Spark."""

    def __init__(self, base_url: str, model: str, timeout: int = 120) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        """Release the underlying HTTP connection pool."""
        await self._client.aclose()

    async def chat(
        self,
        system_prompt: str,
        user_content: str,
        retries: int = 3,
    ) -> str:
        """Send a chat-completion request with automatic retries.

        Args:
            system_prompt: The system message.
            user_content: The user message.
            retries: Maximum number of attempts.

        Returns:
            The assistant's reply text.

        Raises:
            httpx.TimeoutException: If all retries are exhausted.
        """
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                response = await self._client.post(
                    f"{self.base_url}/chat/completions",
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_content},
                        ],
                    },
                )
                response.raise_for_status()
                return response.json()["choices"][0]["message"]["content"]
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_exc = exc
                if attempt < retries:
                    wait = 2 ** attempt
                    logger.warning(
                        "LLM request failed (attempt %d/%d, %s), retrying in %ds",
                        attempt, retries, type(exc).__name__, wait,
                    )
                    await asyncio.sleep(wait)
        raise last_exc  # type: ignore[misc]

    async def analyze_item(self, title: str, content: str) -> AnalysisResult:
        """Score and summarize a single news item.

        Args:
            title: The item title.
            content: The item body text.

        Returns:
            An ``AnalysisResult`` with summary, score, tags, and image prompt.
        """
        user_msg = f"Title: {title}\n\nContent: {content}"
        raw = await self.chat(ANALYZER_SYSTEM_PROMPT, user_msg)

        try:
            cleaned = re.sub(r"```json\s*", "", raw)
            cleaned = re.sub(r"```\s*", "", cleaned)
            parsed = json.loads(cleaned)
            return AnalysisResult(
                summary=parsed.get("summary", raw[:500]),
                relevance_score=max(1, min(10, int(parsed.get("relevance_score", 5)))),
                tags=parsed.get("tags", []),
                image_prompt=parsed.get("image_prompt", "cloud security concept art"),
            )
        except (json.JSONDecodeError, KeyError, ValueError):
            logger.warning("Failed to parse LLM JSON, using fallback")
            return AnalysisResult(
                summary=raw[:500],
                relevance_score=5,
                tags=[],
                image_prompt="cloud security concept art",
            )

    async def generate_briefing(self, items: list[dict]) -> str:
        """Generate a creative editorial briefing from analyzed items.

        Args:
            items: Database records with ``title``, ``url``, ``summary``,
                ``relevance_score``, ``source_type``, and ``raw_data``.

        Returns:
            Markdown text of the complete briefing.
        """
        entries: list[str] = []
        for item in items:
            entry = (
                f"- Title: {item['title']}\n"
                f"  URL: {item['url']}\n"
                f"  Summary: {item['summary']}\n"
                f"  Score: {item['relevance_score']}/10\n"
                f"  Source: {item['source_type']}"
            )
            ref_urls: list[str] = []
            raw = item.get("raw_data")
            if raw:
                if isinstance(raw, str):
                    try:
                        raw = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        raw = {}
                refs = raw.get("references", [])
                seen: set[str] = set()
                for ref in refs:
                    url = ref.get("url", "") if isinstance(ref, dict) else str(ref)
                    if (
                        url
                        and url not in seen
                        and not any(d in url for d in _SKIP_DOMAINS)
                        and url != item["url"]
                    ):
                        seen.add(url)
                        ref_urls.append(url)
            if ref_urls:
                entry += "\n  Reference links:\n" + "\n".join(
                    f"    - {u}" for u in ref_urls[:5]
                )
            else:
                entry += "\n  Reference links: none"
            entries.append(entry)
        user_msg = "Today's items:\n\n" + "\n\n".join(entries)
        return await self.chat(BRIEFING_SYSTEM_PROMPT, user_msg)

    async def generate_cover_prompt(self, topics: str) -> str:
        """Generate a Flux image prompt from today's security topics.

        Args:
            topics: Bullet-list summary of today's items.

        Returns:
            A single-line image generation prompt.
        """
        user_msg = f"Topics:\n{topics}"
        raw = await self.chat(COVER_ART_SYSTEM_PROMPT, user_msg)
        return raw.replace("`", "").strip()
