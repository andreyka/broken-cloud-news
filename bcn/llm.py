from __future__ import annotations

import json
import logging
import re

import httpx

from bcn.models import AnalysisResult

logger = logging.getLogger(__name__)

# Verbatim from n8n/analyzers/main_analyzer.json
ANALYZER_SYSTEM_PROMPT = (
    "You are a Senior Cloud Security Engineer curating the 'Broken Cloud' daily security brief. "
    "Your goal is to identify practical, hands-on security content while filtering out marketing fluff "
    "and theoretical chatter.\n\n"
    "Analyze the following content and return JSON with:\n"
    "1. summary: A concise, technical summary (2-3 sentences) focusing on the 'so what?' for a practitioner.\n"
    "2. juiciness_score: 1-10. High (8-10) for exploits, POCs, novel attacks, or tools. Low (1-4) for marketing/policy.\n"
    "3. tags: Array of 3-5 technical tags.\n"
    "4. image_prompt: A creative, eye-catching image prompt for an AI art generator. "
    "Abstractly represent the core technical concept (e.g., cyberpunk server room, glowing digital shield). "
    "Avoid text. Make it dramatic and high contrast."
)

# Verbatim from n8n/generators/digest_generator.json
COVER_ART_SYSTEM_PROMPT = (
    "You are an AI art director for a security newsletter. Create a single, high-contrast, dramatic "
    "image prompt that abstractly represents the following security topics. Avoid text. Focus on visual "
    "metaphors (e.g., cloud, cybersecurity, data centers, digital storms, cyber shields, cyber warfare, cyberpunk). Be creative. Return ONLY the prompt text."
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
        # Ported from n8n main_analyzer.json "Parse LLM" node
        try:
            cleaned = re.sub(r"```json\s*", "", raw)
            cleaned = re.sub(r"```\s*", "", cleaned)
            parsed = json.loads(cleaned)
            return AnalysisResult(
                summary=parsed.get("summary", raw[:500]),
                juiciness_score=max(1, min(10, int(parsed.get("juiciness_score", 5)))),
                tags=parsed.get("tags", []),
                image_prompt=parsed.get("image_prompt", "cloud security concept art"),
            )
        except (json.JSONDecodeError, KeyError, ValueError):
            logger.warning("Failed to parse LLM JSON, using fallback")
            return AnalysisResult(
                summary=raw[:500],
                juiciness_score=5,
                tags=[],
                image_prompt="cloud security concept art",
            )

    async def generate_cover_prompt(self, topics: str) -> str:
        user_msg = f"Topics:\n{topics}"
        raw = await self.chat(COVER_ART_SYSTEM_PROMPT, user_msg)
        # Strip markdown fences if present
        return raw.replace("`", "").strip()
