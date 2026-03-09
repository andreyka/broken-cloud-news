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


def _make_collector_service(**source_results) -> AsyncMock:
    collector_service = AsyncMock()

    async def _collect(source: str):
        result = source_results.get(source, [])
        if isinstance(result, BaseException):
            raise result
        return result

    collector_service.collect.side_effect = _collect
    return collector_service


@pytest.mark.asyncio
async def test_execute_collection_reports_failed_sources_in_collect_all():
    from bcn.workflows.collection import execute_collection

    settings = _make_settings()
    collector_service = _make_collector_service(
        ghsa=[],
        rss=RuntimeError("rss failure"),
        twitter=[],
        reddit=[],
    )

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
