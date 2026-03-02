"""Telegram distribution channel using the Bot API."""

from __future__ import annotations

import base64
import logging
import re
from typing import Any, Iterable

import httpx

from bcn.common.url_policy import assert_public_http_url
from bcn.common.url_policy import normalize_trusted_hosts
from bcn.common.url_policy import URLValidationError

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

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        overflow_mode: str = "smart",
        trusted_image_hosts: Iterable[str] | None = None,
    ) -> None:
        self.bot_token: str = bot_token
        self.chat_id: str = chat_id
        self.api: str = f"https://api.telegram.org/bot{bot_token}"
        self.overflow_mode: str = overflow_mode
        self._trusted_image_hosts = normalize_trusted_hosts(trusted_image_hosts)
        self._client: httpx.AsyncClient = httpx.AsyncClient(timeout=30)
        self.last_result: dict[str, object] = {}

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def send(
        self,
        briefing: Any,
    ) -> bool:
        """Send a briefing to Telegram as a photo+caption message.

        If the briefing text exceeds the 1024-char caption limit, the
        remainder is sent as a reply to the photo message.  Falls back
        to plain text if no cover image is available.

        Args:
            briefing: The briefing dataset record.

        Returns:
            ``True`` if the message was sent successfully.
        """
        markdown = str(briefing.get("content_markdown") or "")
        cover_image_url = briefing.get("cover_image_url")

        # Strip markdown image tags — Telegram doesn't render them.
        clean_text = re.sub(r"!\[[^\]]*\]\([^)]*\)\n*", "", markdown).strip()
        result: dict[str, object] = {
            "ok": False,
            "chat_id": self.chat_id,
            "message_ids": [],
            "used_cover_image": False,
            "overflow_sent": False,
        }

        try:
            photo_msg_id: int | None = None

            if cover_image_url:
                try:
                    caption_raw = self._truncate_caption(clean_text)
                    caption = caption_raw.strip()
                    overflow = clean_text[len(caption_raw) :].lstrip("\n")
                    if not caption:
                        caption = "Broken Cloud briefing"

                    filename, mime_type, img_bytes = await self._load_cover_image_bytes(
                        cover_image_url
                    )
                    resp = await self._client.post(
                        f"{self.api}/sendPhoto",
                        data={
                            "chat_id": self.chat_id,
                            "caption": caption,
                            "parse_mode": "Markdown",
                        },
                        files={"photo": (filename, img_bytes, mime_type)},
                    )
                    resp.raise_for_status()
                    payload = resp.json()
                    photo_msg_id = payload.get("result", {}).get("message_id")
                    if photo_msg_id is not None:
                        result["used_cover_image"] = True
                        result["caption_chars"] = len(caption)
                        result["message_ids"] = [photo_msg_id]
                except Exception as exc:
                    logger.warning("Failed to send cover photo: %s", exc)
                    result["cover_image_error"] = str(exc)

            if photo_msg_id is not None:
                # Send overflow as a follow-up reply only when useful.
                overflow = clean_text[len(self._truncate_caption(clean_text)) :].lstrip("\n")
                if overflow and self._should_send_overflow(overflow):
                    result["overflow_sent"] = True
                    for chunk in self._split_message(overflow):
                        resp = await self._client.post(
                            f"{self.api}/sendMessage",
                            json={
                                "chat_id": self.chat_id,
                                "text": chunk,
                                "parse_mode": "Markdown",
                                "disable_web_page_preview": True,
                                "reply_to_message_id": photo_msg_id,
                            },
                        )
                        resp.raise_for_status()
                        payload = resp.json()
                        msg_id = payload.get("result", {}).get("message_id")
                        if msg_id is not None and isinstance(result["message_ids"], list):
                            result["message_ids"].append(msg_id)
            else:
                # Fallback: no photo, send as plain text message(s)
                sent_ids: list[int] = []
                for chunk in self._split_message(clean_text):
                    resp = await self._client.post(
                        f"{self.api}/sendMessage",
                        json={
                            "chat_id": self.chat_id,
                            "text": chunk,
                            "parse_mode": "Markdown",
                            "disable_web_page_preview": True,
                        },
                    )
                    resp.raise_for_status()
                    payload = resp.json()
                    msg_id = payload.get("result", {}).get("message_id")
                    if msg_id is not None:
                        sent_ids.append(msg_id)
                result["message_ids"] = sent_ids

            ids = result.get("message_ids")
            if isinstance(ids, list) and ids:
                result["primary_message_id"] = ids[0]
            result["ok"] = True
            self.last_result = result
            return True
        except Exception:
            logger.exception("Telegram send failed")
            result["ok"] = False
            self.last_result = result
            return False

    async def _load_cover_image_bytes(
        self, cover_image_url: str
    ) -> tuple[str, str, bytes]:
        """Load image bytes from an HTTP URL or data URI."""
        if cover_image_url.startswith("data:image/"):
            return self._decode_data_image_uri(cover_image_url)

        try:
            assert_public_http_url(
                cover_image_url,
                trusted_hosts=self._trusted_image_hosts,
            )
        except URLValidationError as exc:
            raise ValueError(f"Blocked cover image URL: {exc}") from exc

        img_resp = await self._client.get(cover_image_url, timeout=30)
        img_resp.raise_for_status()
        content_type = (
            img_resp.headers.get("content-type", "image/png").split(";")[0].strip()
            or "image/png"
        )
        ext = content_type.rsplit("/", 1)[-1] if "/" in content_type else "png"
        filename = f"cover.{ext}"
        return filename, content_type, img_resp.content

    @staticmethod
    def _decode_data_image_uri(value: str) -> tuple[str, str, bytes]:
        """Decode `data:image/...;base64,...` into filename, mime and bytes."""
        header, sep, payload = value.partition(",")
        if not sep or ";base64" not in header:
            raise ValueError("Unsupported data URI format for cover image")
        mime_type = (
            header[5 : header.index(";")] if header.startswith("data:") else "image/png"
        )
        raw = base64.b64decode(payload)
        ext = mime_type.rsplit("/", 1)[-1] if "/" in mime_type else "png"
        return f"cover.{ext}", mime_type, raw

    @staticmethod
    def _truncate_caption(text: str) -> str:
        """Truncate text to fit Telegram's photo caption limit.

        Splits on semantic boundaries and avoids ending on dangling
        section headers.

        Args:
            text: The full message text.

        Returns:
            A string no longer than ``TELEGRAM_MAX_CAPTION`` characters.
        """
        if len(text) <= TELEGRAM_MAX_CAPTION:
            return text
        split_at = TelegramDistributor._find_split_at(text, TELEGRAM_MAX_CAPTION)
        return text[:split_at].rstrip("\n")

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

            split_at = TelegramDistributor._find_split_at(text, TELEGRAM_MAX_MESSAGE)
            if split_at <= 0:
                split_at = TELEGRAM_MAX_MESSAGE

            chunks.append(text[:split_at].rstrip("\n"))
            text = text[split_at:].lstrip("\n")

        return chunks

    @staticmethod
    def _find_split_at(text: str, limit: int) -> int:
        """Find a safe split index near ``limit`` without dangling headings."""
        if len(text) <= limit:
            return len(text)

        min_idx = max(1, int(limit * 0.55))
        para_split = text.rfind("\n\n", 0, limit + 1)
        line_split = text.rfind("\n", 0, limit + 1)

        if para_split >= min_idx:
            split_at = para_split
        elif line_split >= min_idx:
            split_at = line_split
        elif para_split > 0:
            split_at = para_split
        elif line_split > 0:
            split_at = line_split
        else:
            split_at = limit

        split_at = TelegramDistributor._rewind_if_dangling_tail(
            text=text,
            split_at=split_at,
            min_idx=max(1, int(limit * 0.45)),
        )
        return split_at

    @staticmethod
    def _rewind_if_dangling_tail(text: str, split_at: int, min_idx: int) -> int:
        """Rewind split if chunk ends with a heading or incomplete lead-in."""
        current = split_at
        for _ in range(3):
            chunk = text[:current].rstrip("\n")
            if not TelegramDistributor._ends_with_dangling_line(chunk):
                break

            prev_para = text.rfind("\n\n", 0, max(current - 1, 0))
            prev_line = text.rfind("\n", 0, max(current - 1, 0))
            candidate = prev_para if prev_para >= min_idx else prev_line
            if candidate < min_idx:
                break
            current = candidate

        return current

    @staticmethod
    def _ends_with_dangling_line(chunk: str) -> bool:
        """Detect chunk tails that should not be final line of a message."""
        lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
        if not lines:
            return False

        tail = lines[-1]
        if re.fullmatch(r"\*\*[^*].*?\*\*", tail):
            return True
        if tail.endswith(":") and len(tail) <= 100:
            return True
        return False

    def _should_send_overflow(self, overflow: str) -> bool:
        """Decide whether overflow should be posted as a second Telegram message."""
        mode = (self.overflow_mode or "smart").lower()
        if mode == "always":
            return True
        if mode == "never":
            return False

        if re.search(r"https?://", overflow):
            return True
        if re.search(
            r"(cve-\d{4}-\d+|ghsa-|patch|fix|exploit|advisory)", overflow, re.I
        ):
            return True

        # Drop short trailing remarks to avoid needless second messages.
        return len(overflow) >= 320 and overflow.count("\n") >= 2
