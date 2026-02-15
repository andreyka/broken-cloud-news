from __future__ import annotations

import httpx
import pytest
import respx

from bcn.scraper import Scraper


@pytest.fixture
def scraper():
    return Scraper(
        content_limit=500,
        min_content_length=10,
        browserless_url="http://fake-browserless:3000",
    )


def _scrape_response(texts: list[str]) -> httpx.Response:
    """Build a Browserless /scrape response with the given text results."""
    return httpx.Response(
        200,
        json={
            "data": [
                {"results": [{"text": t}]}
                for t in texts
            ]
        },
    )


class TestScraper:
    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_article_content(self, scraper):
        respx.post("http://fake-browserless:3000/scrape").mock(
            return_value=_scrape_response(["This is the article body with enough content."])
        )
        result = await scraper.scrape("https://example.com/post")
        assert "article body" in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_falls_back_to_body(self, scraper):
        route = respx.post("http://fake-browserless:3000/scrape")
        # First call (article/main selectors) returns too-short content
        # Second call (body fallback) returns good content
        route.side_effect = [
            _scrape_response(["short"]),
            _scrape_response(["Fallback body content that is long enough."]),
        ]
        result = await scraper.scrape("https://example.com")
        assert "Fallback body" in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_respects_content_limit(self, scraper):
        long_text = "x" * 1000
        respx.post("http://fake-browserless:3000/scrape").mock(
            return_value=_scrape_response([long_text])
        )
        result = await scraper.scrape("https://example.com")
        assert len(result) == 500

    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_empty_on_http_error(self, scraper):
        respx.post("http://fake-browserless:3000/scrape").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        result = await scraper.scrape("https://example.com")
        assert result == ""

    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_content(self, scraper):
        respx.post("http://fake-browserless:3000/scrape").mock(
            return_value=_scrape_response([""])
        )
        result = await scraper.scrape("https://example.com")
        assert result == ""
