from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class SlackDistributor:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
        self._client = httpx.AsyncClient(timeout=30)

    async def close(self) -> None:
        await self._client.aclose()

    async def send(
        self,
        markdown: str,
        cover_image_url: str | None = None,
    ) -> bool:
        """Send briefing to Slack via webhook using Block Kit."""
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
    def _build_blocks(markdown: str, cover_image_url: str | None) -> list[dict]:
        blocks: list[dict] = []

        if cover_image_url:
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
                "text": {"type": "mrkdwn", "text": chunk},
            })

        return blocks
