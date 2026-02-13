from __future__ import annotations

import json
import logging
import re

import httpx

from bcn.models import AnalysisResult

logger = logging.getLogger(__name__)

# Analyzer system prompt
ANALYZER_SYSTEM_PROMPT = (
    "You are a Senior Cloud Security Engineer curating the 'Broken Cloud' daily security brief. "
    "Your goal is to identify practical, hands-on security content while filtering out marketing fluff "
    "and theoretical chatter.\n\n"
    "Analyze the following content and return JSON with:\n"
    "1. summary: A concise, technical summary (1-2 sentences MAX) focusing on the 'so what?' for a practitioner. Be brief.\n"
    "2. relevance_score: 1-10 based on THESE criteria (not just severity!):\n"
    "   - 9-10: Active exploitation in the wild, public PoC available, or critical vuln widely "
    "     present in cloud workloads (Kubernetes, Docker, AWS/Azure/GCP, Terraform, IAM). "
    "     Must have concrete technical details, not just a CVE number.\n"
    "   - 7-8: Critical/High severity WITH detailed write-up, exploit path described, "
    "     or significant tooling for offensive/defensive security. Affects common cloud components.\n"
    "   - 5-6: Important advisory but lacking detail, or good security content that's not immediately actionable.\n"
    "   - 3-4: Niche/ICS-only vulnerabilities, compliance updates, or vendor-specific without broad impact.\n"
    "   - 1-2: Marketing fluff, policy-only content, or unrelated to cloud/security practice.\n"
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
    "metaphors (e.g., cloud, cybersecurity, data centers, digital storms, cyber shields, cyber warfare, cyberpunk). Be creative. Return ONLY the prompt text."
)


BRIEFING_SYSTEM_PROMPT = (
    "You are the editor-in-chief of 'Broken Cloud Daily Briefing', a punchy cloud-security newsletter "
    "beloved for its sharp wit, clear insight, and no-BS tone.\n\n"
    "You will receive a set of scored security items. Each item has a title, summary, main URL, "
    "and possibly extra reference links (GitHub commits, blog write-ups, etc.).\n\n"
    "Write a single cohesive briefing in Markdown that:\n"
    "1. Opens with a short editorial intro (1-2 sentences) capturing the security mood of the day.\n"
    "2. Groups items by theme with creative section names (e.g., 'Container Chaos', 'IAM Nightmares').\n"
    "3. For each item write ONE sentence — your sharp take on why it matters. "
    "Include the main title as a Markdown hyperlink. "
    "If reference links are provided, pick 1-2 of the MOST interesting ones "
    "(prioritize: blog write-ups with technical details > GitHub PRs/commits showing the fix > "
    "official advisories). Add them as extra inline links like [write-up](url) or [fix](url). "
    "Skip generic NVD/MITRE/errata links.\n"
    "4. Closes with a punchy one-liner sign-off.\n\n"
    "IMPORTANT: Keep the briefing SHORT — under 2500 characters total (Telegram-friendly). "
    "Brevity is a feature. One sentence per item, not two.\n"
    "Do NOT include a title/header line — that will be added by the system. "
    "Do NOT use score numbers or 'Source:' labels. Write for humans, not robots."
)


class LLMClient:
    def __init__(self, base_url: str, model: str, timeout: int = 120):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def chat(self, system_prompt: str, user_content: str) -> str:
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

    async def analyze_item(self, title: str, content: str) -> AnalysisResult:
        user_msg = f"Title: {title}\n\nContent: {content}"
        raw = await self.chat(ANALYZER_SYSTEM_PROMPT, user_msg)

        # JSON parsing with markdown fence stripping + fallback
        # Parse LLM JSON response with fallback
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
        """Generate a creative editorial briefing from analyzed items."""
        skip_domains = {"nvd.nist.gov", "cve.mitre.org", "cve.org", "access.redhat.com"}
        entries = []
        for item in items:
            entry = (
                f"- Title: {item['title']}\n"
                f"  URL: {item['url']}\n"
                f"  Summary: {item['summary']}\n"
                f"  Score: {item['relevance_score']}/10\n"
                f"  Source: {item['source_type']}"
            )
            # Extract reference links from raw_data if available
            ref_urls = []
            raw = item.get("raw_data")
            if raw:
                if isinstance(raw, str):
                    try:
                        raw = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        raw = {}
                refs = raw.get("references", [])
                for ref in refs:
                    url = ref.get("url", "") if isinstance(ref, dict) else str(ref)
                    if url and not any(d in url for d in skip_domains) and url != item["url"]:
                        ref_urls.append(url)
            if ref_urls:
                entry += "\n  Reference links:\n" + "\n".join(f"    - {u}" for u in ref_urls[:5])
            entries.append(entry)
        user_msg = "Today's items:\n\n" + "\n\n".join(entries)
        return await self.chat(BRIEFING_SYSTEM_PROMPT, user_msg)

    async def generate_cover_prompt(self, topics: str) -> str:
        user_msg = f"Topics:\n{topics}"
        raw = await self.chat(COVER_ART_SYSTEM_PROMPT, user_msg)
        # Strip markdown fences if present
        return raw.replace("`", "").strip()
