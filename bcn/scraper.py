from __future__ import annotations

import logging

from playwright.async_api import async_playwright, Browser, BrowserContext

logger = logging.getLogger(__name__)


class Scraper:
    """Fetch page content via Playwright (headless Chromium).

    Launches a persistent headless browser and uses CSS selectors to extract
    article text from ``article``, ``.markdown-body``, ``main``, or ``body``.
    """

    def __init__(
        self,
        content_limit: int = 10000,
        min_content_length: int = 100,
    ):
        self.content_limit = content_limit
        self.min_content_length = min_content_length
        self._pw = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def _ensure_browser(self) -> BrowserContext:
        if self._context is not None:
            return self._context
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)
        self._context = await self._browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            java_script_enabled=True,
        )
        return self._context

    async def close(self) -> None:
        if self._context:
            await self._context.close()
            self._context = None
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._pw:
            await self._pw.stop()
            self._pw = None

    async def scrape(self, url: str) -> str:
        """Navigate to *url* and return extracted article text.

        Tries CSS selectors ``article``, ``.markdown-body``, ``main`` in order,
        falling back to ``body``.  Returns an empty string on any failure.
        """
        context = await self._ensure_browser()
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            # Give dynamic content a moment to render
            await page.wait_for_timeout(2000)

            selectors = ["article", ".markdown-body", "main", "body"]
            for selector in selectors:
                try:
                    el = await page.query_selector(selector)
                    if el is None:
                        continue
                    text = (await el.inner_text()).strip()
                    if text and len(text) >= self.min_content_length:
                        return text[: self.content_limit]
                except Exception:
                    continue

            return ""
        except Exception as exc:
            logger.warning("Playwright scrape failed for %s: %s", url, exc)
            return ""
        finally:
            await page.close()
