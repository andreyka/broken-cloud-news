from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_MAX_MESSAGE = 4096
TELEGRAM_MAX_CAPTION = 1024


class TelegramDistributor:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api = f"https://api.telegram.org/bot{bot_token}"
        self._client = httpx.AsyncClient(timeout=30)

    async def close(self) -> None:
        await self._client.aclose()

    async def send(
        self,
        markdown: str,
        cover_image_url: str | None = None,
    ) -> bool:
        """Send briefing to Telegram. Returns True on success."""
        try:
            # Send cover image first if available
            if cover_image_url:
                # Caption is limited to 1024 chars; send image with short caption
                caption = markdown[:TELEGRAM_MAX_CAPTION] if len(markdown) <= TELEGRAM_MAX_CAPTION else ""
                await self._client.post(
                    f"{self.api}/sendPhoto",
                    json={
                        "chat_id": self.chat_id,
                        "photo": cover_image_url,
                        "caption": caption,
                        "parse_mode": "Markdown",
                    },
                )
                # If caption contained the full message, we're done
                if caption == markdown:
                    return True

            # Send text in chunks respecting 4096 char limit
            chunks = self._split_message(markdown)
            for chunk in chunks:
                await self._client.post(
                    f"{self.api}/sendMessage",
                    json={
                        "chat_id": self.chat_id,
                        "text": chunk,
                        "parse_mode": "Markdown",
                        "disable_web_page_preview": True,
                    },
                )

            return True
        except Exception:
            logger.exception("Telegram send failed")
            return False

    @staticmethod
    def _split_message(text: str) -> list[str]:
        if len(text) <= TELEGRAM_MAX_MESSAGE:
            return [text]

        chunks = []
        while text:
            if len(text) <= TELEGRAM_MAX_MESSAGE:
                chunks.append(text)
                break

            # Try to split at a newline near the limit
            split_at = text.rfind("\n", 0, TELEGRAM_MAX_MESSAGE)
            if split_at == -1:
                split_at = TELEGRAM_MAX_MESSAGE

            chunks.append(text[:split_at])
            text = text[split_at:].lstrip("\n")

        return chunks
