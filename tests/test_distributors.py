from __future__ import annotations

import httpx
import pytest
import respx

from bcn.distributors.telegram import TelegramDistributor
from bcn.distributors.slack import SlackDistributor


class TestTelegramDistributor:
    def test_split_message_short(self):
        chunks = TelegramDistributor._split_message("short message")
        assert chunks == ["short message"]

    def test_split_message_long(self):
        text = "line\n" * 2000  # well over 4096 chars
        chunks = TelegramDistributor._split_message(text)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= 4096

    def test_truncate_caption_short(self):
        assert TelegramDistributor._truncate_caption("short") == "short"

    def test_truncate_caption_long(self):
        text = "a" * 2000
        truncated = TelegramDistributor._truncate_caption(text)
        assert len(truncated) <= 1024

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_text_only(self):
        dist = TelegramDistributor(bot_token="123:FAKE", chat_id="-100")
        respx.post("https://api.telegram.org/bot123:FAKE/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        ok = await dist.send("*Title*\n\nBody text here")
        assert ok is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_with_cover(self):
        dist = TelegramDistributor(bot_token="123:FAKE", chat_id="-100")
        respx.get("http://comfy:8188/view?filename=cover.png").mock(
            return_value=httpx.Response(200, content=b"\x89PNG\r\n")
        )
        respx.post("https://api.telegram.org/bot123:FAKE/sendPhoto").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        respx.post("https://api.telegram.org/bot123:FAKE/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        ok = await dist.send(
            "*Title*\n\nBody text here",
            cover_image_url="http://comfy:8188/view?filename=cover.png",
        )
        assert ok is True


class TestSlackDistributor:
    def test_build_blocks_text_only(self):
        blocks = SlackDistributor._build_blocks("Hello world", None)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "section"
        assert blocks[0]["text"]["text"] == "Hello world"

    def test_build_blocks_with_image(self):
        blocks = SlackDistributor._build_blocks("Hello", "https://img.com/cover.png")
        assert blocks[0]["type"] == "image"
        assert blocks[0]["image_url"] == "https://img.com/cover.png"
        assert blocks[1]["type"] == "section"

    def test_build_blocks_splits_long_text(self):
        long_text = "line\n" * 1500  # well over 3000 chars
        blocks = SlackDistributor._build_blocks(long_text, None)
        assert len(blocks) > 1
        for block in blocks:
            assert len(block["text"]["text"]) <= 3000

    @respx.mock
    @pytest.mark.asyncio
    async def test_send(self):
        dist = SlackDistributor(webhook_url="https://hooks.slack.com/fake")
        respx.post("https://hooks.slack.com/fake").mock(
            return_value=httpx.Response(200, text="ok")
        )

        ok = await dist.send("Briefing content")
        assert ok is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_failure(self):
        dist = SlackDistributor(webhook_url="https://hooks.slack.com/fake")
        respx.post("https://hooks.slack.com/fake").mock(
            return_value=httpx.Response(500, text="error")
        )

        ok = await dist.send("content")
        assert ok is False
