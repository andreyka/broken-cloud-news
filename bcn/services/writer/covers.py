"""Cover-image and artifact assembly helpers for writer workflows."""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


async def build_release_artifact(
    service: Any,
    *,
    briefing_body: str,
    selected_items: list[dict[str, Any]],
    mode: str,
) -> dict[str, str]:
    """Build final markdown/html assets and cover metadata."""
    topics = "\n".join(
        f"- {item['title']}: {item['summary']}" for item in selected_items
    )
    cover_prompt = await service.writer_llm.generate_cover_prompt(topics)
    logger.info("Cover prompt: %s", cover_prompt[:100])

    cover_url = await generate_cover_image(service, cover_prompt)
    return {
        "cover_prompt": cover_prompt,
        "cover_url": cover_url,
        "markdown": service.format_markdown(briefing_body, cover_url, mode=mode),
        "html": service.format_html(briefing_body, cover_url, mode=mode),
    }


async def generate_cover_image(service: Any, cover_prompt: str) -> str:
    """Generate a cover image through the configured writer image backends."""
    cover_url = ""
    if service.writer_llm.supports_cover_image_generation():
        try:
            cover_url = (
                await service.writer_llm.generate_cover_image_data_url(cover_prompt)
                or ""
            )
            if cover_url:
                logger.info("Cover image generated via Gemini image model")
        except Exception:
            logger.exception(
                "Failed to generate Gemini cover image, falling back to ComfyUI"
            )
    if not cover_url:
        try:
            prefix = f"Digest_Cover_{int(time.time() * 1000)}"
            cover_url = await service.comfyui.generate_image(cover_prompt, prefix)
            logger.info("Cover image: %s", cover_url)
        except Exception:
            logger.exception(
                "Failed to generate cover image, continuing without it"
            )
    return cover_url


__all__ = [
    "build_release_artifact",
    "generate_cover_image",
]
