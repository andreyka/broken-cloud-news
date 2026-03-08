from __future__ import annotations

from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from bcn.common.config import Settings


def _make_settings(**overrides) -> Settings:
    defaults = dict(
        database_url="postgresql://test:test@localhost:5432/test",
        llm_base_url="http://fake-llm:8000/v1",
        llm_model="test-model",
        comfyui_url="http://fake-comfy:8188",
        github_token="ghp_fake",
        source_review_enabled=False,
    )
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.mark.asyncio
async def test_execute_collection_reports_failed_sources_in_collect_all():
    from bcn.workflows.collection import execute_collection

    settings = _make_settings()
    collector_service = AsyncMock()
    collector_service.collect_ghsa_items.return_value = []
    collector_service.collect_rss_items.side_effect = RuntimeError("rss failure")
    collector_service.collect_twitter_items.return_value = []
    collector_service.collect_reddit_items.return_value = []

    with patch("bcn.workflows.collection.get_pool", new_callable=AsyncMock), patch(
        "bcn.workflows.collection._persist_collected_items",
        new_callable=AsyncMock,
        side_effect=[1, 3, 4],
    ):
        result = await execute_collection(
            settings,
            source="all",
            collector_service=collector_service,
            origin="test",
            manage_pool=False,
        )

    assert "All: GHSA=1, RSS=0, Twitter=3, Reddit=4" in result
    assert "failures: rss" in result.lower()
