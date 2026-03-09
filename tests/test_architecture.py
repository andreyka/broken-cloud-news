"""Tests for architecture improvements: pool config, concurrency, job isolation, health check."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch, MagicMock
from uuid import uuid4

import pytest

from bcn.common.config import Settings
from bcn.common.models import AnalyzedItemUpdate
from bcn.workflows.analysis import execute_analysis
from bcn.workflows.automation import _run_job_safely


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# 1. Configurable database pool size
# ---------------------------------------------------------------------------

class TestDatabasePoolConfig:
    def test_default_pool_sizes(self):
        s = Settings()
        assert s.database_pool_min_size == 2
        assert s.database_pool_max_size == 20

    def test_custom_pool_sizes(self, monkeypatch):
        monkeypatch.setenv("BCN_DATABASE_POOL_MIN_SIZE", "5")
        monkeypatch.setenv("BCN_DATABASE_POOL_MAX_SIZE", "50")
        s = Settings()
        assert s.database_pool_min_size == 5
        assert s.database_pool_max_size == 50


# ---------------------------------------------------------------------------
# 2. Hardcoded IPs removed from defaults
# ---------------------------------------------------------------------------

class TestConfigNoHardcodedIPs:
    def test_llm_base_url_empty_by_default(self):
        s = Settings()
        assert s.llm_base_url == ""
        assert "192.168" not in s.llm_base_url

    def test_comfyui_url_empty_by_default(self):
        s = Settings()
        assert s.comfyui_url == ""
        assert "192.168" not in s.comfyui_url


# ---------------------------------------------------------------------------
# 3. Health check port configuration
# ---------------------------------------------------------------------------

class TestHealthCheckConfig:
    def test_default_health_check_port(self):
        s = Settings()
        assert s.health_check_port == 8080

    def test_custom_health_check_port(self, monkeypatch):
        monkeypatch.setenv("BCN_HEALTH_CHECK_PORT", "9090")
        s = Settings()
        assert s.health_check_port == 9090


# ---------------------------------------------------------------------------
# 4. Analysis concurrency configuration
# ---------------------------------------------------------------------------

class TestAnalysisConcurrencyConfig:
    def test_default_analysis_concurrency(self):
        s = Settings()
        assert s.analysis_concurrency == 5

    def test_custom_analysis_concurrency(self, monkeypatch):
        monkeypatch.setenv("BCN_ANALYSIS_CONCURRENCY", "10")
        s = Settings()
        assert s.analysis_concurrency == 10


# ---------------------------------------------------------------------------
# 5. Concurrent item analysis
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_analysis_processes_items():
    """Verify analysis processes multiple items concurrently."""
    settings = _make_settings(analysis_concurrency=3)
    item_ids = [uuid4() for _ in range(5)]
    items = [
        {
            "id": iid,
            "title": f"Item {i}",
            "full_content": "Body",
            "url": f"https://example.com/item-{i}",
            "source_type": "rss",
            "source_id": f"rss-{i}",
            "raw_data": {},
            "status": "ANALYZING",
        }
        for i, iid in enumerate(item_ids)
    ]

    call_order = []
    max_concurrent = 0
    active_count = 0
    lock = asyncio.Lock()

    async def _mock_analyze(item):
        nonlocal active_count, max_concurrent
        async with lock:
            active_count += 1
            max_concurrent = max(max_concurrent, active_count)
        call_order.append(item["id"])
        await asyncio.sleep(0.01)  # simulate work
        async with lock:
            active_count -= 1
        return AnalyzedItemUpdate(
            summary="Test summary",
            relevance_score=8,
            ai_tags=["test"],
            full_content="Enriched",
            image_prompt="test prompt",
            canonical_url=f"https://example.com/primary",
        )

    service = _make_analyst_service()
    service.analyze_item = _mock_analyze

    with (
        patch("bcn.workflows.analysis.get_pool", new_callable=AsyncMock),
        patch(
            "bcn.workflows.analysis.get_new_items",
            new_callable=AsyncMock,
            return_value=items,
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

    assert result == "Analyzed 5/5 items"
    assert mock_update.await_count == 5
    assert len(call_order) == 5
    # With concurrency=3 and 5 items, we should see >1 concurrent
    assert max_concurrent > 1


@pytest.mark.asyncio
async def test_concurrent_analysis_respects_semaphore():
    """Verify concurrency is bounded by analysis_concurrency setting."""
    settings = _make_settings(analysis_concurrency=2)
    item_ids = [uuid4() for _ in range(4)]
    items = [
        {
            "id": iid,
            "title": f"Item {i}",
            "full_content": "Body",
            "url": f"https://example.com/item-{i}",
            "source_type": "rss",
            "source_id": f"rss-{i}",
            "raw_data": {},
            "status": "ANALYZING",
        }
        for i, iid in enumerate(item_ids)
    ]

    max_concurrent = 0
    active_count = 0
    lock = asyncio.Lock()

    async def _mock_analyze(item):
        nonlocal active_count, max_concurrent
        async with lock:
            active_count += 1
            max_concurrent = max(max_concurrent, active_count)
        await asyncio.sleep(0.05)
        async with lock:
            active_count -= 1
        return AnalyzedItemUpdate(
            summary="Test",
            relevance_score=7,
            ai_tags=[],
        )

    service = _make_analyst_service()
    service.analyze_item = _mock_analyze

    with (
        patch("bcn.workflows.analysis.get_pool", new_callable=AsyncMock),
        patch(
            "bcn.workflows.analysis.get_new_items",
            new_callable=AsyncMock,
            return_value=items,
        ),
        patch("bcn.workflows.analysis.update_item_analyzed", new_callable=AsyncMock),
        patch("bcn.workflows.analysis.release_items_from_analyzing", new_callable=AsyncMock),
    ):
        result = await execute_analysis(
            settings,
            analyst_service=service,
            manage_pool=False,
        )

    assert result == "Analyzed 4/4 items"
    assert max_concurrent <= 2


@pytest.mark.asyncio
async def test_concurrent_analysis_handles_partial_failures():
    """Verify concurrent analysis correctly counts failures."""
    settings = _make_settings(analysis_concurrency=3)
    item_ids = [uuid4() for _ in range(3)]
    items = [
        {
            "id": iid,
            "title": f"Item {i}",
            "full_content": "Body",
            "url": f"https://example.com/item-{i}",
            "source_type": "rss",
            "source_id": f"rss-{i}",
            "raw_data": {},
            "status": "ANALYZING",
        }
        for i, iid in enumerate(item_ids)
    ]

    call_count = 0

    async def _mock_analyze(item):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("llm down")
        return AnalyzedItemUpdate(
            summary="Test",
            relevance_score=7,
            ai_tags=[],
        )

    service = _make_analyst_service()
    service.analyze_item = _mock_analyze

    with (
        patch("bcn.workflows.analysis.get_pool", new_callable=AsyncMock),
        patch(
            "bcn.workflows.analysis.get_new_items",
            new_callable=AsyncMock,
            return_value=items,
        ),
        patch("bcn.workflows.analysis.update_item_analyzed", new_callable=AsyncMock),
        patch("bcn.workflows.analysis.release_items_from_analyzing", new_callable=AsyncMock),
    ):
        result = await execute_analysis(
            settings,
            analyst_service=service,
            manage_pool=False,
        )

    assert result == "Analyzed 2/3 items (1 failed)"


# ---------------------------------------------------------------------------
# 6. Job error isolation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_job_safely_catches_exceptions():
    """Verify _run_job_safely does not propagate exceptions."""
    async def _failing_coro():
        raise RuntimeError("job failure")

    # Should not raise
    await _run_job_safely("test_job", _failing_coro())


@pytest.mark.asyncio
async def test_run_job_safely_passes_on_success():
    """Verify _run_job_safely works for successful coroutines."""
    result_holder = []

    async def _successful_coro():
        result_holder.append("done")

    await _run_job_safely("test_job", _successful_coro())
    assert result_holder == ["done"]


# ---------------------------------------------------------------------------
# 7. Health check server
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health_server_responds_ok():
    """Verify the health check server responds with 200 OK."""
    from bcn.workflows.service import _start_health_server

    server = await _start_health_server(0)  # random port
    port = server.sockets[0].getsockname()[1]

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        response = await reader.read(1024)
        writer.close()

        assert b"200 OK" in response
        assert b'"ok":true' in response or b'"ok": true' in response
    finally:
        server.close()
        await server.wait_closed()
