"""Playwright-based headless Chromium scraper for extracting article text."""

from __future__ import annotations

import logging
import os
import json
from typing import Iterable, Optional
from urllib.parse import urljoin

from playwright.async_api import async_playwright
from playwright.async_api import Browser
from playwright.async_api import BrowserContext
from playwright.async_api import Playwright
from playwright.async_api import Request as PWRequest
from playwright.async_api import Route as PWRoute

from bcn.common.url_policy import assert_public_http_url
from bcn.common.url_policy import normalize_trusted_hosts
from bcn.common.url_policy import URLValidationError

logger = logging.getLogger(__name__)
_ALIVE_STATUS_CODES = frozenset({200, 401, 403, 405, 429})


class Scraper:
    """Fetch page content via Playwright (headless Chromium).

    Launches a persistent headless browser and uses CSS selectors to extract
    article text from ``article``, ``.markdown-body``, ``main``, or ``body``.
    """

    def __init__(
        self,
        content_limit: int = 10000,
        min_content_length: int = 100,
        trusted_hosts: Iterable[str] | None = None,
    ) -> None:
        self.content_limit = content_limit
        self.min_content_length = min_content_length
        self.trusted_hosts = normalize_trusted_hosts(trusted_hosts)
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None

    async def _ensure_browser(self) -> BrowserContext:
        """Lazily start Playwright and return a reusable browser context."""
        if self._context is not None:
            return self._context
        self._pw = await async_playwright().start()
        launch_kwargs: dict[str, object] = {"headless": True}
        proxy_server = self._playwright_proxy_server()
        if proxy_server:
            launch_kwargs["proxy"] = {"server": proxy_server}
        self._browser = await self._pw.chromium.launch(**launch_kwargs)
        self._context = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            java_script_enabled=True,
        )
        # Enforce SSRF policy on every browser-level request, including redirects
        # and secondary fetches triggered while loading a page.
        await self._context.route("**/*", self._guard_request)
        return self._context

    async def close(self) -> None:
        """Shut down the browser and Playwright runtime."""
        if self._context:
            await self._context.close()
            self._context = None
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._pw:
            await self._pw.stop()
            self._pw = None

    async def __aenter__(self) -> "Scraper":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @staticmethod
    def _playwright_proxy_server() -> str | None:
        """Resolve proxy server URL for Playwright, if configured."""
        for key in ("BCN_PLAYWRIGHT_PROXY", "HTTPS_PROXY", "HTTP_PROXY"):
            value = os.getenv(key, "").strip()
            if value:
                return value
        return None

    @staticmethod
    def _is_http_url(url: str) -> bool:
        text = (url or "").strip().lower()
        return text.startswith("http://") or text.startswith("https://")

    async def _guard_request(self, route: PWRoute, request: PWRequest) -> None:
        """Block internal/private HTTP requests at the browser network layer."""
        request_url = request.url or ""
        if self._is_http_url(request_url):
            try:
                assert_public_http_url(request_url, trusted_hosts=self.trusted_hosts)
            except URLValidationError as exc:
                logger.warning(
                    "Blocked browser subrequest by SSRF policy: %s (%s)",
                    request_url,
                    exc,
                )
                await route.abort("blockedbyclient")
                return
        await route.continue_()

    async def scrape(self, url: str) -> str:
        """Navigate to *url* and return extracted article text.

        Tries CSS selectors ``article``, ``.markdown-body``, ``main`` in
        order, falling back to ``body``.  Returns an empty string on any
        failure.

        Args:
            url: The page URL to scrape.

        Returns:
            Extracted text truncated to ``content_limit``, or ``""``.
        """
        try:
            assert_public_http_url(url, trusted_hosts=self.trusted_hosts)
        except URLValidationError as exc:
            logger.warning("Blocked URL by SSRF policy: %s (%s)", url, exc)
            return ""

        context = await self._ensure_browser()
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
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

    async def fetch_text(
        self,
        url: str,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        timeout_ms: int = 45000,
    ) -> tuple[int, str]:
        """Fetch raw response text through Playwright network stack.

        Useful as a fallback or standard fetch mechanism to bypass simple blocks.

        Args:
            url: Target URL.
            method: HTTP method (e.g., "GET", "HEAD", "POST").
            headers: Optional request headers.
            timeout_ms: Request timeout in milliseconds.

        Returns:
            Tuple of (status_code, response_text). Returns (0, "") on failure.
        """
        try:
            assert_public_http_url(url, trusted_hosts=self.trusted_hosts)
        except URLValidationError as exc:
            logger.warning("Blocked URL by SSRF policy: %s (%s)", url, exc)
            return 0, ""

        context = await self._ensure_browser()
        current_url = url
        current_method = method.upper()
        max_redirects = 5
        redirects = 0
        try:
            while True:
                response = await context.request.fetch(
                    current_url,
                    method=current_method,
                    headers=headers or {},
                    max_redirects=0,
                    timeout=timeout_ms,
                )
                status = int(response.status)

                if status not in {301, 302, 303, 307, 308}:
                    text = await response.text()
                    return status, text

                if redirects >= max_redirects:
                    logger.warning(
                        "Playwright fetch exceeded redirect limit for %s", url
                    )
                    return 0, ""

                location = ""
                try:
                    hdrs = response.headers
                    if isinstance(hdrs, dict):
                        location = str(hdrs.get("location") or "").strip()
                except Exception:
                    location = ""
                if not location:
                    logger.warning(
                        "Playwright fetch redirect missing Location for %s", current_url
                    )
                    return 0, ""

                next_url = urljoin(current_url, location)
                assert_public_http_url(next_url, trusted_hosts=self.trusted_hosts)
                current_url = next_url
                if status in {301, 302, 303} and current_method not in {"GET", "HEAD"}:
                    current_method = "GET"
                redirects += 1
        except URLValidationError as exc:
            logger.warning("Blocked redirect URL by SSRF policy: %s (%s)", url, exc)
            return 0, ""
        except Exception as exc:
            logger.warning("Playwright fetch failed for %s: %s", url, exc)
            return 0, ""

    async def fetch_text_or_raise(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout_ms: int = 45000,
    ) -> str:
        """Fetch text and raise when response is not usable."""
        status, text = await self.fetch_text(
            url=url,
            method="GET",
            headers=headers,
            timeout_ms=timeout_ms,
        )
        if 200 <= status < 400 and text:
            return text
        raise RuntimeError(f"Failed to fetch {url} (status={status})")

    async def fetch_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout_ms: int = 20000,
    ) -> dict:
        """Fetch JSON payload and validate top-level shape."""
        text = await self.fetch_text_or_raise(
            url=url,
            headers=headers,
            timeout_ms=timeout_ms,
        )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Failed to parse JSON from {url}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"Unexpected JSON shape from {url}")
        return payload

    async def is_url_live(
        self,
        url: str,
        *,
        cache: dict[str, bool] | None = None,
        timeout_ms: int = 10000,
    ) -> bool:
        """Check URL liveness with shared HEAD->GET fallback semantics."""
        if not url.startswith(("http://", "https://")):
            return False
        if cache is not None and url in cache:
            return cache[url]

        alive = False
        try:
            status, _ = await self.fetch_text(url, method="HEAD", timeout_ms=timeout_ms)
            alive = status in _ALIVE_STATUS_CODES
            if not alive and status >= 500:
                status, _ = await self.fetch_text(
                    url,
                    method="GET",
                    timeout_ms=timeout_ms,
                )
                alive = status in _ALIVE_STATUS_CODES
        except Exception:
            alive = False

        if cache is not None:
            cache[url] = alive
        return alive
