from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch
from uuid import uuid4

import httpx
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
    assert mock_release.await_args.kwargs["discard_on_exhaustion"] is True


@pytest.mark.asyncio
async def test_execute_analysis_defers_rate_limited_items():
    settings = _make_settings()
    item_id = uuid4()
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(429, request=request)
    service = _make_analyst_service(
        side_effect=httpx.HTTPStatusError(
            "Client error '429 Too Many Requests' for url 'https://api.openai.com/v1/chat/completions'",
            request=request,
            response=response,
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
                    "title": "Rate limited",
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
    assert mock_release.await_args.kwargs["discard_on_exhaustion"] is False


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
