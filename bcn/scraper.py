from __future__ import annotations

import logging

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Selector priority from n8n/scrapers/browserless.json
SELECTORS = ["article", ".markdown-body", "main"]


class Scraper:
    def __init__(self, content_limit: int = 10000, min_content_length: int = 100):
        self.content_limit = content_limit
        self.min_content_length = min_content_length
        self._client = httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": "BCN-Scraper/1.0"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def scrape(self, url: str) -> str:
        """Fetch URL and extract article text. Returns empty string on failure."""
        try:
            response = await self._client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Failed to fetch %s: %s", url, exc)
            return ""

        soup = BeautifulSoup(response.text, "lxml")

        # Try priority selectors (matching browserless.json behavior)
        for selector in SELECTORS:
            element = soup.select_one(selector)
            if element:
                text = element.get_text(separator="\n", strip=True)
                if len(text) >= self.min_content_length:
                    return text[: self.content_limit]

        # Fallback to body
        body = soup.find("body")
        if body:
            text = body.get_text(separator="\n", strip=True)
            if text:
                return text[: self.content_limit]

        return ""
