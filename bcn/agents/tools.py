"""Shared tools for agents to use during LLM generation."""

import logging

from bcn.common.scraper import Scraper

logger = logging.getLogger(__name__)


async def fetch_page_content(url: str) -> str:
    """Fetch the actual page content of a provided URL.
    
    Use this tool when you need more context about an item (like exploit details, 
    affected versions, or patch links) than what is provided in the initial context.
    """
    logger.info("Agent LLM called tool: fetch_page_content(%s)", url)
    scraper = Scraper()
    try:
        content = await scraper.scrape(url)
        return content or "Failed to fetch content or content was too short."
    except Exception as e:
        logger.warning("Tool fetch_page_content failed: %s", e)
        return f"Error fetching content: {e}"
    finally:
        await scraper.close()
