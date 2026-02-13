from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

# Browserless /content endpoint returns the text content of a fully-rendered
# page (JavaScript executed, dynamic content loaded).  We POST a JSON payload
# with the target URL and receive plain text back — no Python-side HTML parsing.

DEFAULT_BROWSERLESS_URL = "http://browserless:3000"


class Scraper:
    """Fetch page content via a Browserless (headless Chromium) service.

    The Browserless ``/content`` endpoint navigates to the URL in a real
    browser, waits for the network to settle, and returns the rendered HTML.
    We then use the ``/scrape`` endpoint with CSS selectors to extract the
    article text server-side — zero Python parsing required.
    """

    def __init__(
        self,
        content_limit: int = 10000,
        min_content_length: int = 100,
        browserless_url: str = DEFAULT_BROWSERLESS_URL,
    ):
        self.content_limit = content_limit
        self.min_content_length = min_content_length
        self.browserless_url = browserless_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=120.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def scrape(self, url: str) -> str:
        """Fetch *url* via Browserless and return extracted article text.

        Uses the ``/scrape`` endpoint with CSS selectors to pull content from
        ``article``, ``.markdown-body``, or ``main`` elements — falling back
        to ``body`` if none match.  Returns an empty string on any failure.
        """
        # First try targeted selectors that typically hold article content
        text = await self._scrape_with_selectors(
            url,
            [
                {"selector": "article"},
                {"selector": ".markdown-body"},
                {"selector": "main"},
            ],
        )

        if text and len(text) >= self.min_content_length:
            return text[: self.content_limit]

        # Fallback: grab the whole <body>
        text = await self._scrape_with_selectors(
            url,
            [{"selector": "body"}],
        )

        if text:
            return text[: self.content_limit]

        return ""

    # --------------------------------------------------------------------- #
    # Internal helpers
    # --------------------------------------------------------------------- #

    async def _scrape_with_selectors(
        self, url: str, elements: list[dict]
    ) -> str:
        """Call Browserless ``/scrape`` and return the first non-empty text."""
        payload = {
            "url": url,
            "elements": elements,
            "gotoOptions": {
                "waitUntil": "networkidle2",
                "timeout": 60000,
            },
        }

        try:
            resp = await self._client.post(
                f"{self.browserless_url}/scrape",
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            logger.warning("Browserless /scrape failed for %s: %s", url, exc)
            return ""
        except Exception as exc:
            logger.warning("Unexpected error scraping %s: %s", url, exc)
            return ""

        # data.data is a list matching the requested selectors
        for item in data.get("data", []):
            results = item.get("results", [])
            for result in results:
                text = result.get("text", "").strip()
                if text and len(text) >= self.min_content_length:
                    return text

        return ""
