"""Discord distributor."""

import logging
from typing import Any
import base64
import httpx

from bcn.common.models import Briefing

logger = logging.getLogger(__name__)

class DiscordDistributor:
    """Sends briefings to a Discord channel via the Discord API."""

    def __init__(self, bot_token: str, channel_id: str) -> None:
        self.bot_token = bot_token.strip()
        self.channel_id = str(channel_id).strip()
        self.api = f"https://discord.com/api/v10/channels/{self.channel_id}/messages"
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bot {self.bot_token}"},
            timeout=30
        )
        self.last_result: dict[str, Any] = {}

    async def send(self, markdown_text: str, image_url: str | None = None) -> bool:
        """Send the briefing to Discord.

        Splits long messages if necessary and attaches the cover image to the first message.
        """
        if not self.bot_token or not self.channel_id:
            logger.warning("Discord distributor skipped: missing token or channel_id")
            return False

        chunks = self._chunk_text(markdown_text, limit=1900)
        
        # Determine image files to send for the first chunk
        files = None
        if image_url:
            if image_url.startswith("data:image/"):
                try:
                    header, sep, payload = image_url.partition(",")
                    mime_type = header[5:header.index(";")] if header.startswith("data:") else "image/png"
                    ext = mime_type.rsplit("/", 1)[-1] if "/" in mime_type else "png"
                    img_bytes = base64.b64decode(payload)
                    files = {"file": (f"cover.{ext}", img_bytes, mime_type)}
                except Exception as e:
                    logger.warning(f"Error parsing data uri: {e}")
            else:
                try:
                    resp = await self._client.get(image_url)
                    resp.raise_for_status()
                    files = {"file": ("cover.png", resp.content, "image/png")}
                except Exception as e:
                    logger.warning(f"Error fetching image: {e}")

        success = True
        first_message_id = None
        
        try:
            import json
            for i, chunk in enumerate(chunks):
                if i == 0 and files:
                    payload = {"payload_json": json.dumps({"content": chunk})}
                    resp = await self._client.post(self.api, data=payload, files=files)
                else:
                    payload = {"content": chunk}
                    resp = await self._client.post(self.api, json=payload)
                    
                resp.raise_for_status()
                data = resp.json()
                
                if i == 0:
                    first_message_id = data.get("id")
                    
            self.last_result = {"primary_message_id": first_message_id}
        except Exception as e:
            logger.error(f"Failed to send Discord message: {e}")
            success = False
        finally:
            await self._client.aclose()
            
        return success

    def _chunk_text(self, text: str, limit: int = 1900) -> list[str]:
        """Split text neatly respecting newlines to fit within Discord's limits."""
        if not text:
            return []
            
        chunks = []
        current_chunk = ""
        
        for paragraph in text.split("\n"):
            if len(current_chunk) + len(paragraph) + 1 <= limit:
                current_chunk += paragraph + "\n"
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                
                if len(paragraph) > limit:
                    for i in range(0, len(paragraph), limit):
                        chunks.append(paragraph[i:i+limit])
                    current_chunk = ""
                else:
                    current_chunk = paragraph + "\n"
                    
        if current_chunk:
            chunks.append(current_chunk.strip())
            
        return chunks
