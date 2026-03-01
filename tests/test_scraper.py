from __future__ import annotations

import socket
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from bcn.common.scraper import Scraper


@pytest.fixture
def scraper():
    return Scraper(content_limit=500, min_content_length=10)


@pytest.fixture(autouse=True)
def _mock_dns(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )


class TestScraper:
    @pytest.mark.asyncio
    async def test_returns_article_content(self, scraper):
        with patch("bcn.common.scraper.async_playwright") as mock_pw_start:
            mock_pw = AsyncMock()
            mock_pw_start.return_value.start = AsyncMock(return_value=mock_pw)

            mock_browser = AsyncMock()
            mock_pw.chromium.launch = AsyncMock(return_value=mock_browser)

            mock_context = AsyncMock()
            mock_browser.new_context = AsyncMock(return_value=mock_context)

            mock_page = AsyncMock()
            mock_context.new_page = AsyncMock(return_value=mock_page)

            mock_el = AsyncMock()
            mock_el.inner_text = AsyncMock(
                return_value="This is the article body with enough content."
            )
            mock_page.query_selector = AsyncMock(return_value=mock_el)

            result = await scraper.scrape("https://example.com/post")
            assert "article body" in result

            mock_page.goto.assert_called_once()
            mock_page.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_falls_back_to_body(self, scraper):
        with patch("bcn.common.scraper.async_playwright") as mock_pw_start:
            mock_pw = AsyncMock()
            mock_pw_start.return_value.start = AsyncMock(return_value=mock_pw)
            mock_browser = AsyncMock()
            mock_pw.chromium.launch = AsyncMock(return_value=mock_browser)
            mock_context = AsyncMock()
            mock_browser.new_context = AsyncMock(return_value=mock_context)
            mock_page = AsyncMock()
            mock_context.new_page = AsyncMock(return_value=mock_page)

            mock_el_short = AsyncMock()
            mock_el_short.inner_text = AsyncMock(return_value="short")

            mock_el_long = AsyncMock()
            mock_el_long.inner_text = AsyncMock(
                return_value="Fallback body content that is long enough."
            )

            # The selectors are ["article", ".markdown-body", "main", "body"]
            # Return short for the first three, long for the last
            mock_page.query_selector = AsyncMock(
                side_effect=[mock_el_short, mock_el_short, mock_el_short, mock_el_long]
            )

            result = await scraper.scrape("https://example.com/post")
            assert "Fallback body" in result

    @pytest.mark.asyncio
    async def test_respects_content_limit(self, scraper):
        with patch("bcn.common.scraper.async_playwright") as mock_pw_start:
            mock_pw = AsyncMock()
            mock_pw_start.return_value.start = AsyncMock(return_value=mock_pw)
            mock_browser = AsyncMock()
            mock_pw.chromium.launch = AsyncMock(return_value=mock_browser)
            mock_context = AsyncMock()
            mock_browser.new_context = AsyncMock(return_value=mock_context)
            mock_page = AsyncMock()
            mock_context.new_page = AsyncMock(return_value=mock_page)

            mock_el = AsyncMock()
            mock_el.inner_text = AsyncMock(return_value="x" * 1000)
            mock_page.query_selector = AsyncMock(return_value=mock_el)

            result = await scraper.scrape("https://example.com/post")
            assert len(result) == 500

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_content(self, scraper):
        with patch("bcn.common.scraper.async_playwright") as mock_pw_start:
            mock_pw = AsyncMock()
            mock_pw_start.return_value.start = AsyncMock(return_value=mock_pw)
            mock_browser = AsyncMock()
            mock_pw.chromium.launch = AsyncMock(return_value=mock_browser)
            mock_context = AsyncMock()
            mock_browser.new_context = AsyncMock(return_value=mock_context)
            mock_page = AsyncMock()
            mock_context.new_page = AsyncMock(return_value=mock_page)

            mock_el = AsyncMock()
            mock_el.inner_text = AsyncMock(return_value="")
            mock_page.query_selector = AsyncMock(return_value=mock_el)

            result = await scraper.scrape("https://example.com/post")
            assert result == ""

    @pytest.mark.asyncio
    async def test_returns_empty_on_exception(self, scraper):
        with patch("bcn.common.scraper.async_playwright") as mock_pw_start:
            mock_pw = AsyncMock()
            mock_pw_start.return_value.start = AsyncMock(return_value=mock_pw)
            mock_browser = AsyncMock()
            mock_pw.chromium.launch = AsyncMock(return_value=mock_browser)
            mock_context = AsyncMock()
            mock_browser.new_context = AsyncMock(return_value=mock_context)
            mock_page = AsyncMock()
            mock_context.new_page = AsyncMock(return_value=mock_page)

            # Make page.goto throw an exception
            mock_page.goto = AsyncMock(side_effect=Exception("Failed to load page"))

            result = await scraper.scrape("https://example.com/post")
            assert result == ""

    @pytest.mark.asyncio
    async def test_fetch_text_returns_status_and_body(self, scraper):
        with patch("bcn.common.scraper.async_playwright") as mock_pw_start:
            mock_pw = AsyncMock()
            mock_pw_start.return_value.start = AsyncMock(return_value=mock_pw)
            mock_browser = AsyncMock()
            mock_pw.chromium.launch = AsyncMock(return_value=mock_browser)
            mock_context = AsyncMock()
            mock_browser.new_context = AsyncMock(return_value=mock_context)
            mock_response = AsyncMock()
            mock_response.status = 200
            mock_response.text = AsyncMock(return_value="feed body")
            mock_response.headers = {}
            mock_context.request.fetch = AsyncMock(return_value=mock_response)

            status, body = await scraper.fetch_text("https://example.com/feed.xml")
            assert status == 200
            assert body == "feed body"
            mock_context.request.fetch.assert_called_once()

    @pytest.mark.asyncio
    async def test_guard_request_blocks_private_subrequest(self, scraper):
        route = AsyncMock()
        request = type("Req", (), {"url": "http://127.0.0.1/admin"})()

        await scraper._guard_request(route, request)

        route.abort.assert_awaited_once()
        route.continue_.assert_not_called()
