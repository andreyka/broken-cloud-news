from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from bcn.distributors.slack import SlackDistributor
import json

from bcn.distributors.substack import SubstackDistributor
from bcn.distributors.telegram import TelegramDistributor


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

    def test_truncate_caption_avoids_dangling_heading(self):
        prefix = ("Complete sentence about previous item.\n" * 30).strip()
        text = (
            prefix
            + "\n\n**Network Protocol Exploits**\n"
            + "[Defending QUIC](https://example.com) patches CVE and gives mitigation steps."
        )
        truncated = TelegramDistributor._truncate_caption(text)
        assert not truncated.rstrip().endswith("**Network Protocol Exploits**")
        overflow = text[len(truncated) :].lstrip("\n")
        assert len(overflow) > 0

    def test_overflow_smart_drops_short_fluff(self):
        dist = TelegramDistributor(
            bot_token="123:FAKE", chat_id="-100", overflow_mode="smart"
        )
        assert dist._should_send_overflow("quick trailing remark") is False

    def test_overflow_smart_keeps_actionable(self):
        dist = TelegramDistributor(
            bot_token="123:FAKE", chat_id="-100", overflow_mode="smart"
        )
        assert (
            dist._should_send_overflow(
                "Patch: CVE-2026-1234 fix here https://example.com"
            )
            is True
        )

    def test_split_message_avoids_heading_tail(self):
        prefix = ("line\n" * 900).strip()
        text = (
            prefix
            + "\n\n**Network Protocol Exploits**\n"
            + "[Defending QUIC](https://example.com) patches CVE and gives mitigation steps."
        )
        chunks = TelegramDistributor._split_message(text)
        assert len(chunks) > 1
        assert not chunks[0].rstrip().endswith("**Network Protocol Exploits**")

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_text_only(self):
        dist = TelegramDistributor(bot_token="123:FAKE", chat_id="-100")
        respx.post("https://api.telegram.org/bot123:FAKE/sendMessage").mock(
            return_value=httpx.Response(
                200, json={"ok": True, "result": {"message_id": 42}}
            )
        )

        ok = await dist.send({"content_markdown": "*Title*\n\nBody text here"})
        assert ok is True
        assert dist.last_result["ok"] is True
        assert dist.last_result["primary_message_id"] == 42

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_with_cover(self):
        dist = TelegramDistributor(
            bot_token="123:FAKE", chat_id="-100", trusted_image_hosts={"comfy"}
        )
        respx.get("http://comfy:8188/view?filename=cover.png").mock(
            return_value=httpx.Response(200, content=b"\x89PNG\r\n")
        )
        respx.post("https://api.telegram.org/bot123:FAKE/sendPhoto").mock(
            return_value=httpx.Response(
                200, json={"ok": True, "result": {"message_id": 101}}
            )
        )
        respx.post("https://api.telegram.org/bot123:FAKE/sendMessage").mock(
            return_value=httpx.Response(
                200, json={"ok": True, "result": {"message_id": 102}}
            )
        )

        ok = await dist.send(
            {
                "content_markdown": "*Title*\n\nBody text here",
                "cover_image_url": "http://comfy:8188/view?filename=cover.png",
            },
        )
        assert ok is True
        assert dist.last_result["ok"] is True
        assert dist.last_result["used_cover_image"] is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_with_data_url_cover(self):
        dist = TelegramDistributor(bot_token="123:FAKE", chat_id="-100")
        respx.post("https://api.telegram.org/bot123:FAKE/sendPhoto").mock(
            return_value=httpx.Response(
                200, json={"ok": True, "result": {"message_id": 201}}
            )
        )
        respx.post("https://api.telegram.org/bot123:FAKE/sendMessage").mock(
            return_value=httpx.Response(
                200, json={"ok": True, "result": {"message_id": 202}}
            )
        )
        data_url = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n").decode(
            "ascii"
        )
        ok = await dist.send(
            {
                "content_markdown": "*Title*\n\nBody text here",
                "cover_image_url": data_url,
            }
        )
        assert ok is True
        assert dist.last_result["used_cover_image"] is True

    @pytest.mark.asyncio
    async def test_send_with_cover_sets_non_empty_caption(self):
        dist = TelegramDistributor(bot_token="123:FAKE", chat_id="-100")
        dist._load_cover_image_bytes = AsyncMock(
            return_value=("cover.png", "image/png", b"\x89PNG\r\n")
        )

        photo_response = httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 301}},
            request=httpx.Request("POST", "https://api.telegram.org/bot123:FAKE/sendPhoto"),
        )

        calls: list[tuple[str, dict]] = []

        async def _fake_post(url: str, **kwargs):
            calls.append((url, kwargs))
            return photo_response

        dist._client.post = AsyncMock(side_effect=_fake_post)

        ok = await dist.send(
            {
                "content_markdown": "*Title*\n\nBody text here",
                "cover_image_url": "http://ignored.example/cover.png",
            }
        )
        assert ok is True
        assert len(calls) == 1
        assert calls[0][0].endswith("/sendPhoto")
        data = calls[0][1]["data"]
        assert data["caption"] == "*Title*\n\nBody text here"
        assert data["parse_mode"] == "Markdown"
        assert dist.last_result["overflow_sent"] is False

    @pytest.mark.asyncio
    async def test_send_with_cover_sends_overflow_followup_when_actionable(self):
        dist = TelegramDistributor(bot_token="123:FAKE", chat_id="-100")
        dist._load_cover_image_bytes = AsyncMock(
            return_value=("cover.png", "image/png", b"\x89PNG\r\n")
        )

        photo_response = httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 401}},
            request=httpx.Request("POST", "https://api.telegram.org/bot123:FAKE/sendPhoto"),
        )
        message_response = httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 402}},
            request=httpx.Request(
                "POST", "https://api.telegram.org/bot123:FAKE/sendMessage"
            ),
        )

        calls: list[tuple[str, dict]] = []

        async def _fake_post(url: str, **kwargs):
            calls.append((url, kwargs))
            if url.endswith("/sendPhoto"):
                return photo_response
            return message_response

        dist._client.post = AsyncMock(side_effect=_fake_post)
        long_actionable = (
            "Intro\n\n"
            + ("line\n" * 300)
            + "\nPatch details: CVE-2026-1234 https://example.com/advisory"
        )
        ok = await dist.send(
            {
                "content_markdown": long_actionable,
                "cover_image_url": "http://ignored.example/cover.png",
            }
        )
        assert ok is True
        assert any(url.endswith("/sendPhoto") for url, _ in calls)
        assert any(url.endswith("/sendMessage") for url, _ in calls)
        assert dist.last_result["overflow_sent"] is True
        assert dist.last_result["message_ids"] == [401, 402]


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

    def test_build_blocks_ignores_data_url_image(self):
        data_url = "data:image/png;base64," + base64.b64encode(b"fake").decode("ascii")
        blocks = SlackDistributor._build_blocks("Hello", data_url)
        assert blocks[0]["type"] == "section"

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

        ok = await dist.send({"content_markdown": "Briefing content"})
        assert ok is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_failure(self):
        dist = SlackDistributor(webhook_url="https://hooks.slack.com/fake")
        respx.post("https://hooks.slack.com/fake").mock(
            return_value=httpx.Response(500, text="error")
        )

        ok = await dist.send({"content_markdown": "content"})
        assert ok is False


class TestSubstackDistributor:
    PUB_URL = "https://testpub.substack.com"

    def _make_dist(self, sid: str = "fake-sid") -> SubstackDistributor:
        return SubstackDistributor(publication_url=self.PUB_URL, sid=sid)

    @staticmethod
    def _patch_page(dist, evaluate_results):
        """Inject a mock page so Playwright is never launched.

        ``evaluate_results`` is a list of return values consumed in order
        by successive ``page.evaluate()`` calls.
        """
        call_index = {"i": 0}

        async def _fake_evaluate(_expr, _arg=None):
            idx = call_index["i"]
            call_index["i"] += 1
            return evaluate_results[idx]

        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(side_effect=_fake_evaluate)
        dist._page = mock_page
        return mock_page

    @pytest.mark.asyncio
    async def test_send_creates_draft_and_publishes(self):
        dist = self._make_dist()
        self._patch_page(dist, [
            {"id": 42},  # draft creation
            {"slug": "broken-cloud-news-2026-03-11"},  # publish
        ])

        ok = await dist.send(
            {
                "content_html": "<p>Briefing body</p>",
                "content_markdown": "Briefing body",
                "email_subject": "BCN Daily - 2026-03-11",
            }
        )
        assert ok is True
        assert dist.last_result["draft_id"] == 42
        assert dist.last_result["post_url"] == (
            f"{self.PUB_URL}/p/broken-cloud-news-2026-03-11"
        )
        assert dist.last_result["primary_message_id"] == dist.last_result["post_url"]

    @pytest.mark.asyncio
    async def test_send_prefers_markdown_over_email_html(self):
        dist = self._make_dist()
        mock_page = self._patch_page(dist, [
            {"id": 1},
            {"slug": "test"},
        ])

        await dist.send(
            {
                "content_html": "<html><body><h1>Email Wrapper</h1><p>Wrong payload</p></body></html>",
                "content_markdown": "**Section Title**\n\nBody with **bold** and [link](https://example.com).",
            }
        )

        # First evaluate call is draft creation; check the body arg
        call_args = mock_page.evaluate.call_args_list[0]
        payload = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("_arg")
        body = json.loads(payload["body"])
        assert body["type"] == "doc"
        assert body["content"][0]["type"] == "heading"
        assert body["content"][0]["attrs"]["level"] == 3
        assert body["content"][0]["content"][0]["text"] == "Section Title"
        paragraph = body["content"][1]["content"]
        assert any(node.get("text") == "bold" for node in paragraph)
        assert any(
            node.get("marks", [{}])[0].get("type") == "link"
            for node in paragraph
            if node.get("text") == "link"
        )

    @pytest.mark.asyncio
    async def test_send_falls_back_to_markdown(self):
        dist = self._make_dist()
        mock_page = self._patch_page(dist, [
            {"id": 1},
            {"slug": "test"},
        ])

        await dist.send({"content_markdown": "# Markdown only"})

        call_args = mock_page.evaluate.call_args_list[0]
        payload = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("_arg")
        body = json.loads(payload["body"])
        assert body["type"] == "doc"
        assert body["content"][0]["type"] == "paragraph"
        assert body["content"][0]["content"][0]["text"] == "# Markdown only"

    @pytest.mark.asyncio
    async def test_send_prepends_cover_image_node(self):
        dist = self._make_dist()
        mock_page = self._patch_page(dist, [
            {"id": 1},
            {"slug": "test"},
        ])

        await dist.send(
            {
                "content_markdown": "Body text here",
                "cover_image_url": "https://img.example/cover.png",
            }
        )

        call_args = mock_page.evaluate.call_args_list[0]
        payload = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("_arg")
        body = json.loads(payload["body"])
        assert body["content"][0] == {
            "type": "image",
            "attrs": {
                "src": "https://img.example/cover.png",
                "alt": "Daily Cover",
            },
        }
        assert body["content"][1]["type"] == "paragraph"

    @pytest.mark.asyncio
    async def test_send_skips_data_url_cover_image_node(self):
        dist = self._make_dist()
        mock_page = self._patch_page(dist, [
            {"id": 1},
            {"slug": "test"},
        ])

        data_url = "data:image/png;base64," + base64.b64encode(b"fake").decode("ascii")
        await dist.send(
            {
                "content_markdown": "Body text here",
                "cover_image_url": data_url,
            }
        )

        call_args = mock_page.evaluate.call_args_list[0]
        payload = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("_arg")
        body = json.loads(payload["body"])
        assert body["content"][0]["type"] == "paragraph"
        assert body["content"][0]["content"][0]["text"] == "Body text here"

    @pytest.mark.asyncio
    async def test_send_failure_on_draft_creation(self):
        dist = self._make_dist()
        self._patch_page(dist, [
            {"error": True, "status": 401, "body": "Unauthorized"},
        ])

        ok = await dist.send({"content_markdown": "content"})
        assert ok is False
        assert "error" in dist.last_result

    @pytest.mark.asyncio
    async def test_send_failure_on_publish(self):
        dist = self._make_dist()
        self._patch_page(dist, [
            {"id": 99},
            {"error": True, "status": 500, "body": "Server Error"},
        ])

        ok = await dist.send({"content_markdown": "content"})
        assert ok is False
        assert "error" in dist.last_result

    def test_extract_title_from_email_subject(self):
        from datetime import datetime
        from datetime import timezone

        dist = self._make_dist(sid="x")
        title = dist._extract_title(
            {
                "email_subject": "Custom Title",
                "created_at": datetime(2026, 3, 11, 23, 6, tzinfo=timezone.utc),
            }
        )
        assert title == "Custom Title (23:06 UTC)"

    def test_extract_title_from_created_at(self):
        from datetime import datetime
        from datetime import timezone

        dist = self._make_dist(sid="x")
        title = dist._extract_title(
            {"created_at": datetime(2026, 3, 11, 23, 6, tzinfo=timezone.utc)}
        )
        assert title == "Broken Cloud News - 2026-03-11 23:06 UTC"

    def test_extract_title_falls_back_to_briefing_id(self):
        dist = self._make_dist(sid="x")
        title = dist._extract_title({"id": "4e59730e-1583-4dcb-82a8-98605478cfbb"})
        assert title == "Broken Cloud News Daily Briefing #4e59730e"

    def test_extract_title_fallback(self):
        dist = self._make_dist(sid="x")
        title = dist._extract_title({})
        assert title == "Broken Cloud News Daily Briefing"

    @pytest.mark.asyncio
    async def test_sid_redacted_in_error(self):
        sid = "super-secret-session-id"
        dist = self._make_dist(sid=sid)
        self._patch_page(dist, [
            {"error": True, "status": 401, "body": f"Invalid session: {sid}"},
        ])

        ok = await dist.send({"content_markdown": "content"})
        assert ok is False
        assert sid not in str(dist.last_result.get("error", ""))
