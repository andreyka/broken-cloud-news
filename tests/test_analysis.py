from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch
from uuid import uuid4

import pytest

from bcn.common.config import Settings
from bcn.common.models import AnalyzedItemUpdate
from bcn.workflows.analysis import execute_analysis


def _make_settings(**overrides) -> Settings:
    defaults = {
        "database_url": "postgresql://test:test@localhost:5432/test",
        "llm_base_url": "http://fake-llm:8000/v1",
        "llm_model": "test-model",
        "comfyui_url": "http://fake-comfy:8188",
        "analysis_retry_max_attempts": 4,
        "analysis_retry_base_delay_seconds": 60,
        "analysis_retry_max_delay_seconds": 600,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _make_analyst_service(*, result=None, side_effect=None):
    service = SimpleNamespace()
    service.analyze_item = AsyncMock(return_value=result, side_effect=side_effect)
    service.close = AsyncMock()
    return service


@pytest.mark.asyncio
async def test_execute_analysis_updates_claimed_items():
    settings = _make_settings()
    item_id = uuid4()
    service = _make_analyst_service(
        result=AnalyzedItemUpdate(
            summary="Container escape in k8s",
            relevance_score=9,
            ai_tags=["k8s"],
            full_content="Enriched body",
            image_prompt="cyberpunk cloud",
            canonical_url="https://example.com/primary",
        )
    )

    with (
        patch("bcn.workflows.analysis.get_pool", new_callable=AsyncMock),
        patch(
            "bcn.workflows.analysis.get_new_items",
            new_callable=AsyncMock,
            return_value=[
                {
                    "id": item_id,
                    "title": "K8s escape",
                    "full_content": "Original body",
                    "url": "https://example.com/item",
                    "source_type": "rss",
                    "source_id": "rss-1",
                    "raw_data": {},
                    "status": "ANALYZING",
                }
            ],
        ),
        patch(
            "bcn.workflows.analysis.update_item_analyzed",
            new_callable=AsyncMock,
        ) as mock_update,
        patch(
            "bcn.workflows.analysis.release_items_from_analyzing",
            new_callable=AsyncMock,
        ) as mock_release,
    ):
        result = await execute_analysis(
            settings,
            analyst_service=service,
            manage_pool=False,
        )

    assert result == "Analyzed 1/1 items"
    service.analyze_item.assert_awaited_once()
    mock_update.assert_awaited_once_with(
        item_id=item_id,
        summary="Container escape in k8s",
        relevance_score=9,
        ai_tags=["k8s"],
        full_content="Enriched body",
        image_prompt="cyberpunk cloud",
        canonical_url="https://example.com/primary",
    )
    mock_release.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_analysis_releases_failed_analyzing_items():
    settings = _make_settings()
    item_id = uuid4()
    service = _make_analyst_service(side_effect=RuntimeError("llm down"))

    with (
        patch("bcn.workflows.analysis.get_pool", new_callable=AsyncMock),
        patch(
            "bcn.workflows.analysis.get_new_items",
            new_callable=AsyncMock,
            return_value=[
                {
                    "id": item_id,
                    "title": "K8s escape",
                    "full_content": "Original body",
                    "url": "https://example.com/item",
                    "source_type": "rss",
                    "source_id": "rss-1",
                    "raw_data": {},
                    "status": "ANALYZING",
                }
            ],
        ),
        patch(
            "bcn.workflows.analysis.update_item_analyzed",
            new_callable=AsyncMock,
        ) as mock_update,
        patch(
            "bcn.workflows.analysis.release_items_from_analyzing",
            new_callable=AsyncMock,
        ) as mock_release,
    ):
        result = await execute_analysis(
            settings,
            analyst_service=service,
            manage_pool=False,
        )

    assert result == "Analyzed 0/1 items (1 failed)"
    mock_update.assert_not_awaited()
    mock_release.assert_awaited_once()
    assert mock_release.await_args.args == ([item_id],)
    assert mock_release.await_args.kwargs["error"] == "RuntimeError: llm down"
    assert mock_release.await_args.kwargs["max_retries"] == 4


@pytest.mark.asyncio
async def test_execute_analysis_returns_no_items_message():
    settings = _make_settings()
    service = _make_analyst_service(
        result=AnalyzedItemUpdate(
            summary="unused",
            relevance_score=5,
            ai_tags=[],
        )
    )

    with (
        patch("bcn.workflows.analysis.get_pool", new_callable=AsyncMock),
        patch(
            "bcn.workflows.analysis.get_new_items",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        result = await execute_analysis(
            settings,
            analyst_service=service,
            manage_pool=False,
        )

    assert result == "No new items to analyze"
    service.analyze_item.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_analysis_processes_multiple_items_concurrently():
    """Multiple items should be dispatched via asyncio.gather, not sequentially."""
    settings = _make_settings(analysis_concurrency=3)
    ids = [uuid4() for _ in range(4)]
    update = AnalyzedItemUpdate(
        summary="ok",
        relevance_score=7,
        ai_tags=["aws"],
    )
    service = _make_analyst_service(result=update)

    rows = [
        {
            "id": ids[i],
            "title": f"Item {i}",
            "full_content": f"Body {i}",
            "url": f"https://example.com/item-{i}",
            "source_type": "rss",
            "source_id": f"rss-{i}",
            "raw_data": {},
            "status": "ANALYZING",
        }
        for i in range(4)
    ]

    with (
        patch("bcn.workflows.analysis.get_pool", new_callable=AsyncMock),
        patch(
            "bcn.workflows.analysis.get_new_items",
            new_callable=AsyncMock,
            return_value=rows,
        ),
        patch(
            "bcn.workflows.analysis.update_item_analyzed",
            new_callable=AsyncMock,
        ) as mock_update,
        patch(
            "bcn.workflows.analysis.release_items_from_analyzing",
            new_callable=AsyncMock,
        ),
    ):
        result = await execute_analysis(
            settings,
            analyst_service=service,
            manage_pool=False,
        )

    assert result == "Analyzed 4/4 items"
    assert service.analyze_item.await_count == 4
    assert mock_update.await_count == 4


@pytest.mark.asyncio
async def test_execute_analysis_mixed_success_and_failure_concurrently():
    """One item fails while others succeed; counts should be correct."""
    settings = _make_settings(analysis_concurrency=2)
    ok_id = uuid4()
    fail_id = uuid4()
    update = AnalyzedItemUpdate(
        summary="ok",
        relevance_score=7,
        ai_tags=[],
    )
    call_count = 0

    async def _side_effect(item):
        nonlocal call_count
        call_count += 1
        if item["id"] == fail_id:
            raise RuntimeError("boom")
        return update

    service = _make_analyst_service()
    service.analyze_item = AsyncMock(side_effect=_side_effect)

    rows = [
        {
            "id": ok_id,
            "title": "Good item",
            "full_content": "content",
            "url": "https://example.com/ok",
            "source_type": "rss",
            "source_id": "rss-ok",
            "raw_data": {},
            "status": "ANALYZING",
        },
        {
            "id": fail_id,
            "title": "Bad item",
            "full_content": "content",
            "url": "https://example.com/fail",
            "source_type": "rss",
            "source_id": "rss-fail",
            "raw_data": {},
            "status": "ANALYZING",
        },
    ]

    with (
        patch("bcn.workflows.analysis.get_pool", new_callable=AsyncMock),
        patch(
            "bcn.workflows.analysis.get_new_items",
            new_callable=AsyncMock,
            return_value=rows,
        ),
        patch(
            "bcn.workflows.analysis.update_item_analyzed",
            new_callable=AsyncMock,
        ) as mock_update,
        patch(
            "bcn.workflows.analysis.release_items_from_analyzing",
            new_callable=AsyncMock,
        ) as mock_release,
    ):
        result = await execute_analysis(
            settings,
            analyst_service=service,
            manage_pool=False,
        )

    assert result == "Analyzed 1/2 items (1 failed)"
    assert mock_update.await_count == 1
    assert mock_release.await_count == 1
