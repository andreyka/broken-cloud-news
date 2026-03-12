"""Substack distribution channel using Playwright to bypass Cloudflare."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional
from urllib.parse import urlsplit

from playwright.async_api import async_playwright
from playwright.async_api import Browser
from playwright.async_api import BrowserContext
from playwright.async_api import Page
from playwright.async_api import Playwright

from bcn.common.secrets import redact_error_text

logger = logging.getLogger(__name__)

# JavaScript executed inside the page context to call the Substack API.
# Using page.evaluate() routes requests through the real browser session,
# which satisfies Cloudflare's JS challenge transparently.
_CREATE_DRAFT_JS = """
async ({title, subtitle, body}) => {
    const userId = window._preloads && window._preloads.user
        ? window._preloads.user.id
        : null;
    if (!userId) {
        return {error: true, status: 0, body: 'Cannot resolve Substack user ID from page preloads'};
    }
    const resp = await fetch('/api/v1/drafts', {
        method: 'POST',
        credentials: 'include',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            draft_title: title,
            draft_subtitle: subtitle,
            draft_body: body,
            type: 'newsletter',
            audience: 'everyone',
            draft_bylines: [{id: userId, is_guest: false}],
        }),
    });
    if (!resp.ok) {
        const text = await resp.text();
        return {error: true, status: resp.status, body: text};
    }
    return await resp.json();
}
"""

_PUBLISH_DRAFT_JS = """
async (draftId) => {
    const resp = await fetch(`/api/v1/drafts/${draftId}/publish`, {
        method: 'POST',
        credentials: 'include',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({send_email: true, audience: 'everyone'}),
    });
    if (!resp.ok) {
        const text = await resp.text();
        return {error: true, status: resp.status, body: text};
    }
    return await resp.json();
}
"""


class SubstackDistributor:
    """Publishes briefings to Substack as newsletter posts.

    Uses Playwright to drive a headless browser, setting the
    ``substack.sid`` session cookie and executing API calls from
    within the page context.  This bypasses Cloudflare's JavaScript
    challenge that blocks plain HTTP clients.

    Attributes:
        publication_url: Base URL of the Substack publication
            (e.g. ``https://mypub.substack.com``).
    """

    def __init__(self, publication_url: str, sid: str) -> None:
        self.publication_url: str = publication_url.rstrip("/")
        self._sid: str = sid
        self._redaction_secrets: tuple[str, ...] = (self._sid,)
        self.last_result: dict[str, Any] = {}

        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._lifecycle_lock = asyncio.Lock()

    async def _ensure_page(self) -> Page:
        """Lazily launch browser, set cookie, navigate, and return a page."""
        if self._page is not None:
            return self._page

        async with self._lifecycle_lock:
            if self._page is not None:
                return self._page

            parsed = urlsplit(self.publication_url)
            domain = parsed.hostname or parsed.netloc

            pw = await async_playwright().start()
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
            )
            await context.add_cookies(
                [
                    {
                        "name": "substack.sid",
                        "value": self._sid,
                        "domain": f".{domain}",
                        "path": "/",
                    }
                ]
            )
            page = await context.new_page()
            await page.goto(self.publication_url, wait_until="domcontentloaded")
            # Allow Cloudflare challenge to resolve
            await page.wait_for_timeout(2000)

            self._pw = pw
            self._browser = browser
            self._context = context
            self._page = page
            return self._page

    async def close(self) -> None:
        """Shut down the browser and Playwright runtime."""
        async with self._lifecycle_lock:
            page = self._page
            context = self._context
            browser = self._browser
            pw = self._pw
            self._page = None
            self._context = None
            self._browser = None
            self._pw = None

            if page:
                await page.close()
            if context:
                await context.close()
            if browser:
                await browser.close()
            if pw:
                await pw.stop()

    async def send(self, briefing: Any) -> bool:
        """Publish a briefing to Substack.

        Creates a draft and immediately publishes it.  Uses
        ``content_html`` with a fallback to ``content_markdown``
        for the post body.

        Args:
            briefing: The briefing dataset record (dict-like).

        Returns:
            ``True`` if the post was published successfully.
        """
        try:
            page = await self._ensure_page()

            title = self._extract_title(briefing)
            body_html = str(
                briefing.get("content_html")
                or briefing.get("content_markdown")
                or ""
            )

            # Step 1: Create a draft
            draft_data = await page.evaluate(
                _CREATE_DRAFT_JS,
                {"title": title, "subtitle": "", "body": body_html},
            )
            if isinstance(draft_data, dict) and draft_data.get("error"):
                raise RuntimeError(
                    f"Draft creation failed (HTTP {draft_data.get('status')}): "
                    f"{draft_data.get('body', '')}"
                )
            draft_id = draft_data["id"]

            # Step 2: Publish the draft
            publish_data = await page.evaluate(_PUBLISH_DRAFT_JS, draft_id)
            if isinstance(publish_data, dict) and publish_data.get("error"):
                raise RuntimeError(
                    f"Publish failed (HTTP {publish_data.get('status')}): "
                    f"{publish_data.get('body', '')}"
                )

            post_url = self._build_post_url(publish_data)
            self.last_result = {
                "draft_id": draft_id,
                "post_url": post_url,
                "primary_message_id": post_url,
            }
            return True
        except Exception as exc:
            safe_error = redact_error_text(
                exc,
                secrets=self._redaction_secrets,
            )
            logger.error("Substack send failed: %s", safe_error)
            self.last_result = {"error": safe_error}
            return False

    def _extract_title(self, briefing: Any) -> str:
        """Derive a post title from the briefing dict."""
        subject = str(briefing.get("email_subject") or "").strip()
        if subject:
            return subject
        created_at = briefing.get("created_at")
        if created_at and hasattr(created_at, "strftime"):
            return f"Broken Cloud News - {created_at.strftime('%Y-%m-%d')}"
        return "Broken Cloud News Daily Briefing"

    def _build_post_url(self, publish_data: dict) -> str | None:
        """Extract or construct the published post URL."""
        slug = publish_data.get("slug")
        if slug:
            return f"{self.publication_url}/p/{slug}"
        canonical = publish_data.get("canonical_url")
        if canonical:
            return str(canonical)
        return None
