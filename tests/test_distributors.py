from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from bcn.distributors.ghost import GhostDistributor
from bcn.distributors.slack import SlackDistributor
import json

from bcn.distributors.substack import SubstackDistributor
from bcn.distributors.telegram import TelegramDistributor


class TestTelegramDistributor:
    def test_render_telegram_text_strips_markdown_entities(self):
        text = (
            "**Alert**\n"
            "[ConsentFix](https://example.com/consentfix)\n"
            "Use `AF_ALG` and inspect `_dmarc`."
        )
        rendered = TelegramDistributor._render_telegram_text(text)
        assert rendered == (
            "Alert\n"
            "ConsentFix: https://example.com/consentfix\n"
            "Use AF_ALG and inspect _dmarc."
        )

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
        assert data["caption"] == "Title\n\nBody text here"
        assert "parse_mode" not in data
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
        message_payloads = [kwargs["json"] for url, kwargs in calls if url.endswith("/sendMessage")]
        assert message_payloads
        assert "parse_mode" not in message_payloads[0]
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


class TestGhostDistributor:
    API_URL = "https://testpub.ghost.io"
    ADMIN_KEY = "ghost-id:" + ("1f" * 32)

    def _make_dist(self, admin_key: str | None = None) -> GhostDistributor:
        return GhostDistributor(
            admin_api_url=self.API_URL,
            admin_api_key=admin_key or self.ADMIN_KEY,
            trusted_image_hosts={"img.example"},
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_publishes_html_post(self):
        dist = self._make_dist()
        route = respx.post(
            "https://testpub.ghost.io/ghost/api/admin/posts/?source=html"
        ).mock(
            return_value=httpx.Response(
                201,
                json={
                    "posts": [
                        {
                            "id": "42",
                            "url": "https://testpub.ghost.io/bcn-daily/",
                            "status": "published",
                        }
                    ]
                },
            )
        )

        ok = await dist.send(
            {
                "content_markdown": "**Section Title**\n\nBody with **bold** and [link](https://example.com).",
                "email_subject": "BCN Daily - 2026-03-11",
            }
        )

        assert ok is True
        assert dist.last_result["post_id"] == "42"
        assert dist.last_result["post_url"] == "https://testpub.ghost.io/bcn-daily/"
        assert dist.last_result["primary_message_id"] == "https://testpub.ghost.io/bcn-daily/"

        request = route.calls[0].request
        assert request.headers["Authorization"].startswith("Ghost ")
        assert request.headers["Accept-Version"] == "v6.0"
        payload = json.loads(request.content.decode("utf-8"))
        post = payload["posts"][0]
        assert post["title"] == "BCN Daily - 2026-03-11"
        assert post["status"] == "published"
        assert "<h3" in post["html"]
        assert "Section Title" in post["html"]
        assert "https://example.com" in post["html"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_uploads_http_cover_as_feature_image(self):
        dist = self._make_dist()
        respx.get("https://img.example/cover.png").mock(
            return_value=httpx.Response(200, content=b"fake", headers={"content-type": "image/png"})
        )
        upload_route = respx.post(
            "https://testpub.ghost.io/ghost/api/admin/images/upload/"
        ).mock(
            return_value=httpx.Response(
                201,
                json={"images": [{"url": "https://cdn.ghost.io/content/images/cover.png"}]},
            )
        )
        route = respx.post(
            "https://testpub.ghost.io/ghost/api/admin/posts/?source=html"
        ).mock(
            return_value=httpx.Response(
                201, json={"posts": [{"id": "1", "status": "published"}]}
            )
        )

        ok = await dist.send(
            {
                "content_markdown": "Body text here",
                "cover_image_url": "https://img.example/cover.png",
            }
        )

        assert ok is True
        assert upload_route.called
        payload = json.loads(route.calls[0].request.content.decode("utf-8"))
        assert (
            payload["posts"][0]["feature_image"]
            == "https://cdn.ghost.io/content/images/cover.png"
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_uploads_data_url_cover(self):
        dist = self._make_dist()
        upload_route = respx.post(
            "https://testpub.ghost.io/ghost/api/admin/images/upload/"
        ).mock(
            return_value=httpx.Response(
                201,
                json={"images": [{"url": "https://cdn.ghost.io/content/images/data-cover.png"}]},
            )
        )
        route = respx.post(
            "https://testpub.ghost.io/ghost/api/admin/posts/?source=html"
        ).mock(
            return_value=httpx.Response(
                201, json={"posts": [{"id": "1", "status": "published"}]}
            )
        )

        data_url = "data:image/png;base64," + base64.b64encode(b"fake").decode("ascii")
        ok = await dist.send(
            {
                "content_markdown": "Body text here",
                "cover_image_url": data_url,
            }
        )

        assert ok is True
        assert upload_route.called
        payload = json.loads(route.calls[0].request.content.decode("utf-8"))
        assert (
            payload["posts"][0]["feature_image"]
            == "https://cdn.ghost.io/content/images/data-cover.png"
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_skips_feature_image_when_upload_fails(self):
        dist = self._make_dist()
        respx.get("https://img.example/cover.png").mock(
            return_value=httpx.Response(200, content=b"fake", headers={"content-type": "image/png"})
        )
        respx.post("https://testpub.ghost.io/ghost/api/admin/images/upload/").mock(
            return_value=httpx.Response(500, text="nope")
        )
        route = respx.post(
            "https://testpub.ghost.io/ghost/api/admin/posts/?source=html"
        ).mock(
            return_value=httpx.Response(
                201, json={"posts": [{"id": "1", "status": "published"}]}
            )
        )

        ok = await dist.send(
            {
                "content_markdown": "Body text here",
                "cover_image_url": "https://img.example/cover.png",
            }
        )

        assert ok is True
        payload = json.loads(route.calls[0].request.content.decode("utf-8"))
        assert "feature_image" not in payload["posts"][0]
        assert "feature_image_error" in dist.last_result

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_retries_feature_image_upload_after_timeout(self):
        dist = self._make_dist()
        respx.get("https://img.example/cover.png").mock(
            return_value=httpx.Response(
                200,
                content=b"fake",
                headers={"content-type": "image/png"},
            )
        )
        upload_route = respx.post(
            "https://testpub.ghost.io/ghost/api/admin/images/upload/"
        ).mock(
            side_effect=[
                httpx.ReadTimeout("slow upload"),
                httpx.Response(
                    201,
                    json={
                        "images": [
                            {"url": "https://cdn.ghost.io/content/images/retried-cover.png"}
                        ]
                    },
                ),
            ]
        )
        route = respx.post(
            "https://testpub.ghost.io/ghost/api/admin/posts/?source=html"
        ).mock(
            return_value=httpx.Response(
                201, json={"posts": [{"id": "1", "status": "published"}]}
            )
        )

        ok = await dist.send(
            {
                "content_markdown": "Body text here",
                "cover_image_url": "https://img.example/cover.png",
            }
        )

        assert ok is True
        assert upload_route.call_count == 2
        payload = json.loads(route.calls[0].request.content.decode("utf-8"))
        assert (
            payload["posts"][0]["feature_image"]
            == "https://cdn.ghost.io/content/images/retried-cover.png"
        )
        assert "feature_image_error" not in dist.last_result

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_failure_on_api_error(self):
        dist = self._make_dist()
        respx.post("https://testpub.ghost.io/ghost/api/admin/posts/?source=html").mock(
            return_value=httpx.Response(401, text="Unauthorized")
        )

        ok = await dist.send({"content_markdown": "content"})
        assert ok is False
        assert "error" in dist.last_result

    def test_extract_title_from_email_subject(self):
        from datetime import datetime
        from datetime import timezone

        dist = self._make_dist()
        title = dist._extract_title(
            {
                "email_subject": "Custom Title",
                "created_at": datetime(2026, 3, 11, 23, 6, tzinfo=timezone.utc),
            }
        )
        assert title == "Custom Title (2026-03-11 23:06 UTC)"

    def test_extract_title_from_created_at(self):
        from datetime import datetime
        from datetime import timezone

        dist = self._make_dist()
        title = dist._extract_title(
            {"created_at": datetime(2026, 3, 11, 23, 6, tzinfo=timezone.utc)}
        )
        assert title == "Broken Cloud Update - 2026-03-11 23:06 UTC"

    def test_extract_title_falls_back_to_briefing_id(self):
        dist = self._make_dist()
        title = dist._extract_title({"id": "4e59730e-1583-4dcb-82a8-98605478cfbb"})
        assert title == "Broken Cloud Update #4e59730e"

    def test_extract_title_fallback(self):
        dist = self._make_dist()
        title = dist._extract_title({})
        assert title == "Broken Cloud Update"

    @pytest.mark.asyncio
    async def test_admin_key_redacted_in_error(self):
        admin_key = "ghost-id:" + ("ab" * 32)
        dist = self._make_dist(admin_key=admin_key)
        dist._client.post = AsyncMock(
            side_effect=RuntimeError(f"Invalid Ghost key {admin_key}")
        )

        ok = await dist.send({"content_markdown": "content"})
        assert ok is False
        assert admin_key not in str(dist.last_result.get("error", ""))


class TestSubstackDistributor:
    PUB_URL = "https://testpub.substack.com"

    def _make_dist(
        self,
        sid: str = "fake-sid",
        *,
        ghost_admin_api_url: str = "",
        ghost_admin_api_key: str = "",
    ) -> SubstackDistributor:
        return SubstackDistributor(
            publication_url=self.PUB_URL,
            sid=sid,
            trusted_image_hosts=("comfy",),
            ghost_admin_api_url=ghost_admin_api_url,
            ghost_admin_api_key=ghost_admin_api_key,
        )

    @staticmethod
    def _patch_page(
        dist: SubstackDistributor,
        evaluate_results: list[dict[str, object]],
    ) -> AsyncMock:
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
        self._patch_page(
            dist,
            [
                {"id": 42},
                {"slug": "broken-cloud-news-2026-03-11"},
            ],
        )

        ok = await dist.send(
            {
                "content_html": "<p>Briefing body</p>",
                "content_markdown": "Briefing body",
                "email_subject": "BCN Daily - 2026-03-11",
            }
        )

        assert ok is True
        assert dist.last_result["draft_id"] == 42
        assert (
            dist.last_result["post_url"]
            == "https://testpub.substack.com/p/broken-cloud-news-2026-03-11"
        )
        assert (
            dist.last_result["primary_message_id"]
            == "https://testpub.substack.com/p/broken-cloud-news-2026-03-11"
        )

    @pytest.mark.asyncio
    async def test_send_skips_unsupported_data_url_cover(self):
        dist = self._make_dist()
        page = self._patch_page(
            dist,
            [
                {"id": 9},
                {"slug": "daily-post"},
            ],
        )

        ok = await dist.send(
            {
                "content_markdown": "Hello world",
                "cover_image_url": "data:image/png;base64,ZmFrZQ==",
            }
        )

        assert ok is True
        args, _kwargs = page.evaluate.await_args_list[0]
        payload = args[1]
        assert "data:image/png" not in payload["body"]
        assert "Hello world" in payload["body"]

    @pytest.mark.asyncio
    async def test_send_hosts_non_public_cover_via_ghost(self):
        dist = self._make_dist(
            ghost_admin_api_url="https://ghost.example",
            ghost_admin_api_key="ghost-id:" + ("ab" * 32),
        )
        page = self._patch_page(
            dist,
            [
                {"id": 9},
                {"slug": "daily-post"},
            ],
        )
        assert dist._ghost_image_host is not None
        dist._ghost_image_host._load_cover_image_bytes = AsyncMock(
            return_value=("cover.png", "image/png", b"\x89PNG\r\n")
        )
        dist._ghost_image_host._upload_image = AsyncMock(
            return_value="https://cdn.ghost.io/content/images/substack-cover.png"
        )

        ok = await dist.send(
            {
                "content_markdown": "Hello world",
                "cover_image_url": "http://comfy:8188/view?filename=cover.png",
            }
        )

        assert ok is True
        args, _kwargs = page.evaluate.await_args_list[0]
        payload = args[1]
        assert "https://cdn.ghost.io/content/images/substack-cover.png" in payload["body"]
        assert dist.last_result["feature_image_url"] == (
            "https://cdn.ghost.io/content/images/substack-cover.png"
        )

    @pytest.mark.asyncio
    async def test_send_reports_safe_error(self):
        dist = self._make_dist(sid="super-secret-substack-sid")
        self._patch_page(
            dist,
            [
                {
                    "error": True,
                    "status": 403,
                    "body": "bad cookie super-secret-substack-sid",
                }
            ],
        )

        ok = await dist.send({"content_markdown": "Body"})

        assert ok is False
        assert "super-secret-substack-sid" not in json.dumps(dist.last_result)
