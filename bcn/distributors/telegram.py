"""Telegram distribution channel using the Bot API."""

from __future__ import annotations

import logging
import re

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_MAX_MESSAGE: int = 4096
TELEGRAM_MAX_CAPTION: int = 1024


class TelegramDistributor:
    """Sends briefings to a Telegram chat via the Bot API.

    Supports photo+caption messages with automatic overflow into reply
    messages when the text exceeds Telegram's 1024-char caption limit.

    Attributes:
        bot_token: Telegram bot authentication token.
        chat_id: Target chat or channel ID.
        api: Base URL for the Telegram Bot API.
    """

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.bot_token: str = bot_token
        self.chat_id: str = chat_id
        self.api: str = f"https://api.telegram.org/bot{bot_token}"
        self._client: httpx.AsyncClient = httpx.AsyncClient(timeout=30)

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def send(
        self,
        markdown: str,
        cover_image_url: str | None = None,
    ) -> bool:
        """Send a briefing to Telegram as a photo+caption message.

        If the briefing text exceeds the 1024-char caption limit, the
        remainder is sent as a reply to the photo message.  Falls back
        to plain text if no cover image is available.

        Args:
            markdown: Briefing text in Markdown format.
            cover_image_url: Optional URL to a cover image.

        Returns:
            ``True`` if the message was sent successfully.
        """

        # Strip markdown image tags — Telegram doesn't render them
        clean_text = re.sub(r"!\[[^\]]*\]\([^)]*\)\n*", "", markdown)

        try:
            photo_msg_id: int | None = None

            if cover_image_url:
                caption = self._truncate_caption(clean_text)
                try:
                    img_resp = await self._client.get(cover_image_url, timeout=30)
                    img_resp.raise_for_status()
                    img_bytes = img_resp.content
                    resp = await self._client.post(
                        f"{self.api}/sendPhoto",
                        data={
                            "chat_id": self.chat_id,
                            "caption": caption,
                            "parse_mode": "Markdown",
                        },
                        files={"photo": ("cover.png", img_bytes, "image/png")},
                    )
                    resp.raise_for_status()
                    photo_msg_id = resp.json().get("result", {}).get("message_id")
                except Exception as exc:
                    logger.warning("Failed to send cover photo: %s", exc)

            if photo_msg_id is not None:
                # Send overflow text (beyond caption limit) as a reply
                overflow = clean_text[len(self._truncate_caption(clean_text)):].lstrip("\n")
                if overflow:
                    for chunk in self._split_message(overflow):
                        await self._client.post(
                            f"{self.api}/sendMessage",
                            json={
                                "chat_id": self.chat_id,
                                "text": chunk,
                                "parse_mode": "Markdown",
                                "disable_web_page_preview": True,
                                "reply_to_message_id": photo_msg_id,
                            },
                        )
            else:
                # Fallback: no photo, send as plain text message(s)
                for chunk in self._split_message(clean_text):
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
        """Truncate text to fit Telegram's photo caption limit.

        Splits at the last newline before the limit so that words are
        not cut mid-line.

        Args:
            text: The full message text.

        Returns:
            A string no longer than ``TELEGRAM_MAX_CAPTION`` characters.
        """
        if len(text) <= TELEGRAM_MAX_CAPTION:
            return text
        split_at = text.rfind("\n", 0, TELEGRAM_MAX_CAPTION)
        if split_at == -1:
            split_at = TELEGRAM_MAX_CAPTION
        return text[:split_at]

    @staticmethod
    def _split_message(text: str) -> list[str]:
        """Split text into chunks that fit Telegram's message size limit.

        Prefers splitting at newline boundaries to keep paragraphs intact.

        Args:
            text: The full message text.

        Returns:
            A list of text chunks, each at most ``TELEGRAM_MAX_MESSAGE``
            characters long.
        """
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
