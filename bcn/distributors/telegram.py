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
        import re

        # Strip markdown image tags — Telegram doesn't render them
        clean_text = re.sub(r"!\[[^\]]*\]\([^)]*\)\n*", "", markdown)

        try:
            photo_sent = False
            if cover_image_url:
                # Extract the title line for the photo caption
                title_line = clean_text.split("\n")[0] if clean_text else ""
                try:
                    img_resp = await self._client.get(cover_image_url, timeout=30)
                    img_resp.raise_for_status()
                    img_bytes = img_resp.content
                    resp = await self._client.post(
                        f"{self.api}/sendPhoto",
                        data={
                            "chat_id": self.chat_id,
                            "caption": title_line,
                            "parse_mode": "Markdown",
                        },
                        files={"photo": ("cover.png", img_bytes, "image/png")},
                    )
                    resp.raise_for_status()
                    photo_sent = True
                except Exception as exc:
                    logger.warning("Failed to send cover photo: %s", exc)

            # Send the briefing body as a single message (skip title if photo had it)
            body = clean_text
            if photo_sent:
                # Remove the title line since it was in the photo caption
                lines = clean_text.split("\n", 1)
                body = lines[1].lstrip("\n") if len(lines) > 1 else ""

            if body:
                for chunk in self._split_message(body):
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
    def _truncate_caption(text: str) -> str:
        """Truncate text to fit Telegram's photo caption limit, splitting at a newline."""
        if len(text) <= TELEGRAM_MAX_CAPTION:
            return text
        split_at = text.rfind("\n", 0, TELEGRAM_MAX_CAPTION)
        if split_at == -1:
            split_at = TELEGRAM_MAX_CAPTION
        return text[:split_at]

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
