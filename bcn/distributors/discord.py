"""Discord distributor."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, Iterable

import httpx

from bcn.common.secrets import redact_error_text
from bcn.common.url_policy import assert_public_http_url
from bcn.common.url_policy import normalize_trusted_hosts
from bcn.common.url_policy import URLValidationError

logger = logging.getLogger(__name__)


class DiscordDistributor:
    """Sends briefings to a Discord channel via the Discord API."""

    def __init__(
        self,
        bot_token: str,
        channel_id: str,
        trusted_image_hosts: Iterable[str] | None = None,
    ) -> None:
        self.bot_token = bot_token.strip()
        self.channel_id = str(channel_id).strip()
        self.api = f"https://discord.com/api/v10/channels/{self.channel_id}/messages"
        self._trusted_image_hosts = normalize_trusted_hosts(trusted_image_hosts)
        self._redaction_secrets: tuple[str, ...] = (self.bot_token,)
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bot {self.bot_token}"},
            timeout=30,
        )
        self.last_result: dict[str, Any] = {}

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def send(self, briefing: Any) -> bool:
        """Send the briefing to Discord.

        Splits long messages if necessary and attaches the cover image
        to the first message.

        Args:
            briefing: The briefing dataset record.

        Returns:
            ``True`` if the message was sent successfully.
        """
        if not self.bot_token or not self.channel_id:
            logger.warning("Discord distributor skipped: missing token or channel_id")
            return False

        markdown_text = str(briefing.get("content_markdown") or "")
        image_url = briefing.get("cover_image_url")

        chunks = self._chunk_text(markdown_text, limit=1900)

        # Determine image files to send for the first chunk
        files = None
        attachments = None
        if image_url:
            if image_url.startswith("data:image/"):
                try:
                    header, sep, payload = image_url.partition(",")
                    mime_type = (
                        header[5 : header.index(";")]
                        if header.startswith("data:")
                        else "image/png"
                    )
                    ext = mime_type.rsplit("/", 1)[-1] if "/" in mime_type else "png"
                    img_bytes = base64.b64decode(payload)
                    filename = f"cover.{ext}"
                    files = {"files[0]": (filename, img_bytes, mime_type)}
                    attachments = [{"id": 0, "filename": filename}]
                except Exception as exc:
                    logger.warning(
                        "Error parsing data uri: %s",
                        redact_error_text(exc, secrets=self._redaction_secrets),
                    )
            else:
                try:
                    assert_public_http_url(
                        image_url,
                        trusted_hosts=self._trusted_image_hosts,
                    )
                    resp = await self._client.get(image_url)
                    resp.raise_for_status()
                    filename = "cover.png"
                    files = {"files[0]": (filename, resp.content, "image/png")}
                    attachments = [{"id": 0, "filename": filename}]
                except URLValidationError as exc:
                    logger.warning(
                        "Blocked cover image URL: %s",
                        redact_error_text(exc, secrets=self._redaction_secrets),
                    )
                except Exception as exc:
                    logger.warning(
                        "Error fetching image: %s",
                        redact_error_text(exc, secrets=self._redaction_secrets),
                    )

        success = True
        first_message_id = None

        try:
            for i, chunk in enumerate(chunks):
                if i == 0 and files:
                    payload_dict: dict[str, Any] = {"content": chunk}
                    if attachments:
                        payload_dict["attachments"] = attachments
                    files["payload_json"] = (
                        None,
                        json.dumps(payload_dict),
                        "application/json",
                    )
                    resp = await self._client.post(self.api, files=files)

                    # Remove payload_json so it isn't re-sent
                    files.pop("payload_json", None)
                else:
                    payload_body = {"content": chunk}
                    resp = await self._client.post(self.api, json=payload_body)

                resp.raise_for_status()
                data = resp.json()

                if i == 0:
                    first_message_id = data.get("id")

            self.last_result = {"primary_message_id": first_message_id}
        except Exception as exc:
            safe_error = redact_error_text(
                exc,
                secrets=self._redaction_secrets,
            )
            logger.error("Failed to send Discord message: %s", safe_error)
            self.last_result = {
                "primary_message_id": first_message_id,
                "error": safe_error,
            }
            success = False

        return success

    def _chunk_text(self, text: str, limit: int = 1900) -> list[str]:
        """Split text neatly respecting newlines to fit within Discord's limits."""
        if not text:
            return []

        chunks: list[str] = []
        current_chunk = ""

        for paragraph in text.split("\n"):
            if len(current_chunk) + len(paragraph) + 1 <= limit:
                current_chunk += paragraph + "\n"
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())

                if len(paragraph) > limit:
                    for i in range(0, len(paragraph), limit):
                        chunks.append(paragraph[i : i + limit])
                    current_chunk = ""
                else:
                    current_chunk = paragraph + "\n"

        if current_chunk:
            chunks.append(current_chunk.strip())

        return chunks
