"""Slack distribution channel using incoming webhooks."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class SlackDistributor:
    """Sends briefings to Slack via an incoming webhook.

    Uses Block Kit to format messages with optional cover images and
    automatic chunking for Slack's 3000-char section limit.

    Attributes:
        webhook_url: Slack incoming webhook URL.
    """

    def __init__(self, webhook_url: str) -> None:
        self.webhook_url: str = webhook_url
        self._client: httpx.AsyncClient = httpx.AsyncClient(timeout=30)

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def send(
        self,
        markdown: str,
        cover_image_url: str | None = None,
    ) -> bool:
        """Send a briefing to Slack via the configured webhook.

        Args:
            markdown: Briefing text in Markdown format.
            cover_image_url: Optional URL to a cover image.

        Returns:
            ``True`` if the webhook request succeeded.
        """
        try:
            blocks = self._build_blocks(markdown, cover_image_url)
            resp = await self._client.post(
                self.webhook_url,
                json={"blocks": blocks},
            )
            resp.raise_for_status()
            return True
        except Exception:
            logger.exception("Slack send failed")
            return False

    @staticmethod
    def _build_blocks(
        markdown: str,
        cover_image_url: str | None,
    ) -> list[dict[str, object]]:
        """Build Slack Block Kit blocks from briefing content.

        Splits the Markdown text into 3000-char sections (Slack's limit)
        and optionally prepends a cover image block.

        Args:
            markdown: Briefing text in Markdown format.
            cover_image_url: Optional URL to a cover image.

        Returns:
            A list of Slack Block Kit block dicts.
        """
        blocks: list[dict[str, object]] = []

        if cover_image_url and cover_image_url.startswith(
            ("http://", "https://")):
            blocks.append({
                "type": "image",
                "image_url": cover_image_url,
                "alt_text": "Broken Cloud News Daily Cover",
            })

        # Slack markdown blocks have a 3000 char limit per section
        chunks = []
        remaining = markdown
        while remaining:
            if len(remaining) <= 3000:
                chunks.append(remaining)
                break
            split_at = remaining.rfind("\n", 0, 3000)
            if split_at == -1:
                split_at = 3000
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:].lstrip("\n")

        for chunk in chunks:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": chunk
                },
            })

        return blocks
