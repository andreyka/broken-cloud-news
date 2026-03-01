"""Shared tools for agents to use during LLM generation."""

from contextlib import contextmanager
from contextvars import ContextVar
import logging
from typing import Iterable

from bcn.briefing.text import canonical_url_key
from bcn.common.scraper import Scraper
from bcn.common.url_policy import assert_public_http_url
from bcn.common.url_policy import URLValidationError

logger = logging.getLogger(__name__)
_allowed_tool_url_keys: ContextVar[frozenset[str]] = ContextVar(
    "allowed_tool_url_keys",
    default=frozenset(),
)


@contextmanager
def allow_tool_urls(urls: Iterable[str] | None):
    """Temporarily allow tool fetches only for the provided URL set."""
    keys = frozenset(
        key for key in (canonical_url_key(str(u)) for u in (urls or [])) if key
    )
    token = _allowed_tool_url_keys.set(keys)
    try:
        yield
    finally:
        _allowed_tool_url_keys.reset(token)


async def fetch_page_content(url: str) -> str:
    """Fetch the actual page content of a provided URL.

    Use this tool when you need more context about an item (like exploit details,
    affected versions, or patch links) than what is provided in the initial context.
    """
    logger.info("Agent LLM called tool: fetch_page_content(%s)", url)
    try:
        assert_public_http_url(url)
    except URLValidationError as exc:
        logger.warning("Tool fetch_page_content blocked URL: %s (%s)", url, exc)
        return "Blocked URL by security policy."

    key = canonical_url_key(url)
    allowed = _allowed_tool_url_keys.get()
    if not key or key not in allowed:
        logger.warning("Tool fetch_page_content blocked non-allowlisted URL: %s", url)
        return "Blocked URL by security policy."

    scraper = Scraper()
    try:
        content = await scraper.scrape(url)
        return content or "Failed to fetch content or content was too short."
    except Exception as e:
        logger.warning("Tool fetch_page_content failed: %s", e)
        return f"Error fetching content: {e}"
    finally:
        await scraper.close()
