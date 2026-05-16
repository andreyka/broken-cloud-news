"""Substack distribution channel using Playwright to bypass Cloudflare."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Iterable
from datetime import datetime
from datetime import timezone
import json
import logging
import os
import re
from typing import Any
from typing import Optional
from urllib.parse import urlsplit

import httpx
from playwright.async_api import async_playwright
from playwright.async_api import Browser
from playwright.async_api import BrowserContext
from playwright.async_api import Page
from playwright.async_api import Playwright

from bcn.common.secrets import redact_error_text
from bcn.common.url_policy import assert_public_http_url
from bcn.common.url_policy import normalize_trusted_hosts
from bcn.common.url_policy import URLValidationError
from bcn.distributors.ghost import GhostDistributor

logger = logging.getLogger(__name__)

_IMAGE_BLOCK_RE = re.compile(r"^!\[(?P<alt>[^\]]*)\]\((?P<src>[^)]+)\)$")
_BOLD_HEADING_RE = re.compile(r"^\*\*(?P<text>.+?)\*\*$")
_INLINE_TOKEN_RE = re.compile(
    r"\[(?P<link_text>[^\]]+)\]\((?P<link_href>[^)]+)\)"
    r"|(?P<bold>\*\*[^*].+?\*\*)"
    r"|(?P<code>`[^`]+`)"
    r"|(?P<italic>\*[^*\n][^*\n]*\*)"
)

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

_UPLOAD_IMAGE_JS = """
async ({image}) => {
    const resp = await fetch('/api/v1/image', {
        method: 'POST',
        credentials: 'include',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({image}),
    });
    if (!resp.ok) {
        const text = await resp.text();
        return {error: true, status: resp.status, body: text};
    }
    return await resp.json();
}
"""

_SUBSTACK_IMAGE_FETCH_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


def _build_substack_body(briefing: Any, *, cover_url: str = "") -> str:
    """Render BCN markdown into Substack's ProseMirror-style document JSON."""
    markdown = str(briefing.get("content_markdown") or "").strip()
    if cover_url:
        markdown = _prepend_cover_block(markdown, cover_url=cover_url)

    if not markdown:
        fallback = str(briefing.get("content_html") or "").strip()
        markdown = _strip_html(fallback)

    blocks = [block.strip() for block in markdown.split("\n\n") if block.strip()]
    content: list[dict[str, Any]] = []
    for block in blocks:
        image = _parse_image_block(block)
        if image:
            content.append(image)
            continue

        heading = _parse_heading_block(block)
        if heading:
            content.append(heading)
            continue

        content.append(_paragraph_node(_parse_inline_nodes(block.replace("\n", " "))))

    if not content:
        content.append(_paragraph_node([{"type": "text", "text": "Broken Cloud News"}]))

    return json.dumps({"type": "doc", "content": content}, separators=(",", ":"))


def _strip_html(html_text: str) -> str:
    """Fallback HTML simplifier when markdown is unavailable."""
    if not html_text:
        return ""
    text = re.sub(r"(?is)<(script|style).*?>.*?</\\1>", "", html_text)
    text = re.sub(r"(?i)<br\\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\\s*>", "\n\n", text)
    text = re.sub(r"(?i)</h[1-6]\\s*>", "\n\n", text)
    text = re.sub(r"(?is)<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _prepend_cover_block(markdown: str, *, cover_url: str) -> str:
    """Ensure the resolved cover image renders as the first block."""
    blocks = [block.strip() for block in str(markdown or "").split("\n\n") if block.strip()]
    if blocks and _parse_image_block(blocks[0]):
        blocks = blocks[1:]
    body = "\n\n".join(blocks).strip()
    cover_block = f"![Daily Cover]({cover_url})"
    if not body:
        return cover_block
    return f"{cover_block}\n\n{body}"


def _parse_image_block(block: str) -> dict[str, Any] | None:
    match = _IMAGE_BLOCK_RE.match(block.strip())
    if not match:
        return None
    src = match.group("src").strip()
    if not src.startswith(("http://", "https://")):
        return None
    return {
        "type": "image",
        "attrs": {
            "src": src,
            "alt": match.group("alt").strip(),
        },
    }


def _parse_heading_block(block: str) -> dict[str, Any] | None:
    match = _BOLD_HEADING_RE.match(block.strip())
    if not match:
        return None
    return {
        "type": "heading",
        "attrs": {"level": 3},
        "content": [{"type": "text", "text": match.group("text").strip()}],
    }


def _paragraph_node(content: list[dict[str, Any]]) -> dict[str, Any]:
    if not content:
        content = [{"type": "text", "text": ""}]
    return {"type": "paragraph", "content": content}


def _parse_inline_nodes(text: str) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    position = 0
    for match in _INLINE_TOKEN_RE.finditer(text):
        start, end = match.span()
        if start > position:
            nodes.append({"type": "text", "text": text[position:start]})
        if match.group("link_text") is not None:
            nodes.append(
                {
                    "type": "text",
                    "text": match.group("link_text"),
                    "marks": [
                        {
                            "type": "link",
                            "attrs": {"href": match.group("link_href")},
                        }
                    ],
                }
            )
        elif match.group("bold") is not None:
            nodes.append(
                {
                    "type": "text",
                    "text": match.group("bold")[2:-2],
                    "marks": [{"type": "strong"}],
                }
            )
        elif match.group("italic") is not None:
            nodes.append(
                {
                    "type": "text",
                    "text": match.group("italic")[1:-1],
                    "marks": [{"type": "em"}],
                }
            )
        elif match.group("code") is not None:
            nodes.append(
                {
                    "type": "text",
                    "text": match.group("code")[1:-1],
                    "marks": [{"type": "code"}],
                }
            )
        position = end
    if position < len(text):
        nodes.append({"type": "text", "text": text[position:]})
    return nodes


class SubstackDistributor:
    """Publishes briefings to Substack as newsletter posts."""

    def __init__(
        self,
        publication_url: str,
        sid: str,
        *,
        trusted_image_hosts: Iterable[str] | None = None,
        ghost_admin_api_url: str = "",
        ghost_admin_api_key: str = "",
    ) -> None:
        self.publication_url: str = publication_url.rstrip("/")
        self._sid: str = sid
        self._trusted_image_hosts = normalize_trusted_hosts(trusted_image_hosts)
        self._http = httpx.AsyncClient(timeout=_SUBSTACK_IMAGE_FETCH_TIMEOUT)
        self._ghost_image_host: GhostDistributor | None = None
        if str(ghost_admin_api_url or "").strip() and str(ghost_admin_api_key or "").strip():
            self._ghost_image_host = GhostDistributor(
                admin_api_url=ghost_admin_api_url,
                admin_api_key=ghost_admin_api_key,
                trusted_image_hosts=trusted_image_hosts,
            )
        self._redaction_secrets: tuple[str, ...] = tuple(
            value
            for value in (self._sid, str(ghost_admin_api_key or "").strip())
            if value
        )
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
            launch_kwargs: dict[str, object] = {"headless": True}
            proxy_server = self._playwright_proxy_server()
            if proxy_server:
                launch_kwargs["proxy"] = {"server": proxy_server}
            browser = await pw.chromium.launch(**launch_kwargs)
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
            await self._http.aclose()
            if self._ghost_image_host is not None:
                await self._ghost_image_host.close()

    @staticmethod
    def _playwright_proxy_server() -> str | None:
        """Resolve proxy server URL for Playwright, if configured."""
        for key in ("BCN_PLAYWRIGHT_PROXY", "HTTPS_PROXY", "HTTP_PROXY"):
            value = os.getenv(key, "").strip()
            if value:
                return value
        return None

    async def send(self, briefing: Any) -> bool:
        """Publish a briefing to Substack."""
        self.last_result = {}
        try:
            page = await self._ensure_page()

            title = self._extract_title(briefing)
            cover_url = await self._prepare_cover_image_url(briefing)
            cover_image_error = str(self.last_result.get("feature_image_error") or "")
            body = _build_substack_body(briefing, cover_url=cover_url)

            draft_data = await page.evaluate(
                _CREATE_DRAFT_JS,
                {"title": title, "subtitle": "", "body": body},
            )
            if isinstance(draft_data, dict) and draft_data.get("error"):
                raise RuntimeError(
                    f"Draft creation failed (HTTP {draft_data.get('status')}): "
                    f"{draft_data.get('body', '')}"
                )
            draft_id = draft_data["id"]

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
            if cover_url:
                self.last_result["feature_image_url"] = cover_url
            if cover_image_error:
                self.last_result["feature_image_error"] = cover_image_error
            return True
        except Exception as exc:
            safe_error = redact_error_text(exc, secrets=self._redaction_secrets)
            logger.error("Substack send failed: %s", safe_error)
            self.last_result = {"error": safe_error}
            return False

    async def _prepare_cover_image_url(self, briefing: Any) -> str:
        """Return a publicly embeddable cover image URL for Substack."""
        cover_image_url = str(briefing.get("cover_image_url") or "").strip()
        if not cover_image_url:
            return ""

        if self._is_public_http_url(cover_image_url):
            return cover_image_url

        native_error = ""
        try:
            image_data_url = await self._load_cover_image_data_url(cover_image_url)
            return await self._upload_image_to_substack(image_data_url)
        except Exception as exc:
            native_error = redact_error_text(exc, secrets=self._redaction_secrets)
            logger.warning("Substack native cover image upload skipped: %s", native_error)

        if self._ghost_image_host is None:
            self.last_result["feature_image_error"] = (
                native_error
                or "Substack cover image requires a public URL or upload path"
            )
            logger.info("Skipping Substack cover image without public hosting path")
            return ""

        try:
            filename, mime_type, image_bytes = (
                await self._ghost_image_host._load_cover_image_bytes(cover_image_url)
            )
            return await self._ghost_image_host._upload_image(
                filename=filename,
                mime_type=mime_type,
                image_bytes=image_bytes,
            )
        except Exception as exc:
            safe_error = redact_error_text(exc, secrets=self._redaction_secrets)
            logger.warning("Substack cover image upload skipped: %s", safe_error)
            if native_error:
                safe_error = f"{native_error}; ghost_fallback={safe_error}"
            self.last_result["feature_image_error"] = safe_error
            return ""

    async def _load_cover_image_data_url(self, cover_image_url: str) -> str:
        """Return cover bytes normalized as a base64 data URL for Substack upload."""
        if cover_image_url.startswith("data:image/"):
            filename, mime_type, image_bytes = self._decode_data_image_uri(
                cover_image_url
            )
        else:
            try:
                assert_public_http_url(
                    cover_image_url,
                    trusted_hosts=self._trusted_image_hosts,
                )
            except URLValidationError as exc:
                raise ValueError(f"Blocked cover image URL: {exc}") from exc

            image_response = await self._http.get(cover_image_url)
            image_response.raise_for_status()
            mime_type = (
                image_response.headers.get("content-type", "image/png")
                .split(";", 1)[0]
                .strip()
                or "image/png"
            )
            image_bytes = image_response.content
            filename = "cover"
        if not image_bytes:
            raise ValueError("empty cover image payload")
        _ = filename  # keep structure aligned with Ghost cover loading semantics
        encoded = base64.b64encode(image_bytes).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    async def _upload_image_to_substack(self, image_data_url: str) -> str:
        """Upload one data-URL cover image via Substack's authenticated image API."""
        page = await self._ensure_page()
        upload_data = await page.evaluate(_UPLOAD_IMAGE_JS, {"image": image_data_url})
        if isinstance(upload_data, dict) and upload_data.get("error"):
            raise RuntimeError(
                f"Substack image upload failed (HTTP {upload_data.get('status')}): "
                f"{upload_data.get('body', '')}"
            )
        uploaded_url = str((upload_data or {}).get("url") or "").strip()
        if not uploaded_url:
            raise ValueError("Substack image upload returned no URL")
        assert_public_http_url(uploaded_url)
        return uploaded_url

    @staticmethod
    def _is_public_http_url(url: str) -> bool:
        """Return whether *url* is directly embeddable by Substack."""
        try:
            assert_public_http_url(url)
            return True
        except URLValidationError:
            return False

    def _extract_title(self, briefing: Any) -> str:
        """Derive a post title from the briefing dict."""
        subject = str(briefing.get("email_subject") or "").strip()
        created_at = briefing.get("created_at")
        briefing_id = str(briefing.get("id") or "").strip()
        if subject:
            suffix = self._title_uniqueness_suffix(created_at, briefing_id)
            return f"{subject} ({suffix})" if suffix else subject
        if isinstance(created_at, datetime):
            return f"Broken Cloud News - {self._format_created_at(created_at)}"
        if briefing_id:
            return f"Broken Cloud News Daily Briefing #{briefing_id[:8]}"
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

    @staticmethod
    def _title_uniqueness_suffix(created_at: Any, briefing_id: str) -> str:
        """Return a stable suffix that makes repeated daily titles unique."""
        if isinstance(created_at, datetime):
            return SubstackDistributor._format_created_time(created_at)
        if briefing_id:
            return briefing_id[:8]
        return ""

    @staticmethod
    def _format_created_at(created_at: datetime) -> str:
        """Render a human-readable date/time label for Substack titles."""
        return created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    @staticmethod
    def _format_created_time(created_at: datetime) -> str:
        """Render the time-only suffix used when a subject already exists."""
        return created_at.astimezone(timezone.utc).strftime("%H:%M UTC")

    @staticmethod
    def _decode_data_image_uri(value: str) -> tuple[str, str, bytes]:
        header, sep, payload = value.partition(",")
        if not sep or ";base64" not in header:
            raise ValueError("Unsupported data URI format for cover image")
        mime_type = (
            header[5 : header.index(";")] if header.startswith("data:") else "image/png"
        )
        raw = base64.b64decode(payload)
        extension = mime_type.rsplit("/", 1)[-1] if "/" in mime_type else "png"
        return f"cover.{extension}", mime_type, raw
