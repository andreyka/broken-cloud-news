from __future__ import annotations

from datetime import datetime
from datetime import timezone
from unittest.mock import AsyncMock
from unittest.mock import Mock
from uuid import uuid4

import pytest

from bcn.agents.collector.review import SourceReviewLLM
from bcn.common.config import Settings
from bcn.common.models import CollectedNewsItem
from bcn.common.models import CollectionSourceReview
from bcn.workflows.collection import execute_collection


def _make_settings(**overrides) -> Settings:
    defaults = {
        "database_url": "postgresql://test:test@localhost:5432/test",
        "llm_base_url": "http://fake-llm:8000/v1",
        "llm_model": "test-model",
        "comfyui_url": "http://fake-comfy:8188",
        "source_review_enabled": False,
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.mark.asyncio
async def test_execute_collection_persists_single_source(monkeypatch):
    settings = _make_settings()
    collected_item = CollectedNewsItem(
        source_type="ghsa",
        source_id="GHSA-test-0001",
        url="https://github.com/advisories/GHSA-test-0001",
        title="Critical kubernetes vuln",
        published_at="2026-01-01T00:00:00Z",
        raw_data={"ghsaId": "GHSA-test-0001"},
        full_content="Advisory details",
    )
    collector_service = AsyncMock()
    collector_service.collect_ghsa_items.return_value = [collected_item]

    insert_mock = AsyncMock(return_value=uuid4())
    monkeypatch.setattr("bcn.workflows.collection.get_pool", AsyncMock())
    monkeypatch.setattr("bcn.workflows.collection.insert_news_item", insert_mock)

    result = await execute_collection(
        settings,
        source="ghsa",
        collector_service=collector_service,
        origin="test",
        manage_pool=False,
    )

    assert result == "GHSA: collected 1 items"
    insert_mock.assert_awaited_once_with(
        source_type="ghsa",
        source_id="GHSA-test-0001",
        url="https://github.com/advisories/GHSA-test-0001",
        title="Critical kubernetes vuln",
        published_at=collected_item.published_at,
        raw_data={"ghsaId": "GHSA-test-0001"},
        full_content="Advisory details",
    )


@pytest.mark.asyncio
async def test_execute_collection_closes_owned_resources(monkeypatch):
    settings = _make_settings()
    collector_service = AsyncMock()
    collector_service.collect_reddit_items.return_value = []
    collector_ctor = Mock(return_value=collector_service)
    close_pool_mock = AsyncMock()

    monkeypatch.setattr("bcn.workflows.collection.get_pool", AsyncMock())
    monkeypatch.setattr("bcn.workflows.collection.close_pool", close_pool_mock)
    monkeypatch.setattr("bcn.workflows.collection.CollectorService", collector_ctor)

    result = await execute_collection(
        settings,
        source="reddit",
        origin="test",
        manage_pool=True,
    )

    assert result == "Reddit: collected 0 items"
    collector_ctor.assert_called_once_with(settings)
    collector_service.close.assert_awaited_once()
    close_pool_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_collection_promotes_new_source_after_review(monkeypatch):
    settings = _make_settings(source_review_enabled=True)
    collected_item = CollectedNewsItem(
        source_type="rss",
        source_id="rss-1",
        url="https://example.com/advisory",
        title="Cloud advisory",
        published_at="2026-01-01T00:00:00Z",
        raw_data={"feed_url": "https://example.com/feed.xml", "summary": "technical"},
        full_content="Details",
    )
    collector_service = AsyncMock()
    collector_service.collect_rss_items.return_value = [collected_item]

    insert_mock = AsyncMock(return_value=uuid4())
    upsert_mock = AsyncMock()
    review_mock = AsyncMock(
        return_value=CollectionSourceReview(
            decision="promote",
            confidence="high",
            rationale="Technical cloud-security feed.",
            signals=["technical samples", "cloud scope"],
        )
    )

    monkeypatch.setattr("bcn.workflows.collection.get_pool", AsyncMock())
    monkeypatch.setattr("bcn.workflows.collection.insert_news_item", insert_mock)
    monkeypatch.setattr(
        "bcn.workflows.collection.get_collection_source",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "bcn.workflows.collection.collection_source_has_historical_items",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "bcn.workflows.collection.upsert_collection_source",
        upsert_mock,
    )
    monkeypatch.setattr(
        "bcn.workflows.collection.record_collection_source_review",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "bcn.workflows.collection.SourceReviewLLM.review_source",
        review_mock,
    )

    result = await execute_collection(
        settings,
        source="rss",
        collector_service=collector_service,
        origin="test",
        manage_pool=False,
    )

    assert result == "RSS: collected 1 items"
    insert_mock.assert_awaited_once()
    assert any(
        call.kwargs.get("state") == "ACTIVE" for call in upsert_mock.await_args_list
    )
    review_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_collection_quarantines_new_source_when_review_rejects(
    monkeypatch,
):
    settings = _make_settings(source_review_enabled=True)
    collected_item = CollectedNewsItem(
        source_type="rss",
        source_id="rss-1",
        url="https://example.com/advisory",
        title="Cloud advisory",
        published_at="2026-01-01T00:00:00Z",
        raw_data={"feed_url": "https://example.com/feed.xml", "summary": "marketing"},
        full_content="Details",
    )
    collector_service = AsyncMock()
    collector_service.collect_rss_items.return_value = [collected_item]

    insert_mock = AsyncMock(return_value=uuid4())
    upsert_mock = AsyncMock()
    review_record_mock = AsyncMock()
    review_mock = AsyncMock(
        return_value=CollectionSourceReview(
            decision="quarantine",
            confidence="high",
            rationale="Looks like marketing.",
            signals=["low technical depth"],
        )
    )

    monkeypatch.setattr("bcn.workflows.collection.get_pool", AsyncMock())
    monkeypatch.setattr("bcn.workflows.collection.insert_news_item", insert_mock)
    monkeypatch.setattr(
        "bcn.workflows.collection.get_collection_source",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "bcn.workflows.collection.collection_source_has_historical_items",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "bcn.workflows.collection.upsert_collection_source",
        upsert_mock,
    )
    monkeypatch.setattr(
        "bcn.workflows.collection.record_collection_source_review",
        review_record_mock,
    )
    monkeypatch.setattr(
        "bcn.workflows.collection.SourceReviewLLM.review_source",
        review_mock,
    )

    result = await execute_collection(
        settings,
        source="rss",
        collector_service=collector_service,
        origin="test",
        manage_pool=False,
    )

    assert result == "RSS: collected 0 items"
    insert_mock.assert_not_awaited()
    review_record_mock.assert_awaited_once()
    assert any(
        call.kwargs.get("state") == "QUARANTINED"
        for call in upsert_mock.await_args_list
    )


@pytest.mark.asyncio
async def test_execute_collection_keeps_new_source_pending_when_review_errors(
    monkeypatch,
):
    settings = _make_settings(source_review_enabled=True)
    collected_item = CollectedNewsItem(
        source_type="rss",
        source_id="rss-1",
        url="https://example.com/advisory",
        title="Cloud advisory",
        published_at="2026-01-01T00:00:00Z",
        raw_data={"feed_url": "https://example.com/feed.xml", "summary": "technical"},
        full_content="Details",
    )
    collector_service = AsyncMock()
    collector_service.collect_rss_items.return_value = [collected_item]

    insert_mock = AsyncMock(return_value=uuid4())
    upsert_mock = AsyncMock()
    review_record_mock = AsyncMock()
    review_mock = AsyncMock(side_effect=RuntimeError("llm timeout"))

    monkeypatch.setattr("bcn.workflows.collection.get_pool", AsyncMock())
    monkeypatch.setattr("bcn.workflows.collection.insert_news_item", insert_mock)
    monkeypatch.setattr(
        "bcn.workflows.collection.get_collection_source",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "bcn.workflows.collection.collection_source_has_historical_items",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "bcn.workflows.collection.upsert_collection_source",
        upsert_mock,
    )
    monkeypatch.setattr(
        "bcn.workflows.collection.record_collection_source_review",
        review_record_mock,
    )
    monkeypatch.setattr(
        "bcn.workflows.collection.SourceReviewLLM.review_source",
        review_mock,
    )

    result = await execute_collection(
        settings,
        source="rss",
        collector_service=collector_service,
        origin="test",
        manage_pool=False,
    )

    assert result == "RSS: collected 0 items"
    insert_mock.assert_not_awaited()
    review_record_mock.assert_not_awaited()
    assert any(
        call.kwargs.get("state") == "PENDING_REVIEW"
        for call in upsert_mock.await_args_list
    )


@pytest.mark.asyncio
async def test_execute_collection_honors_quarantined_source_when_review_disabled(
    monkeypatch,
):
    settings = _make_settings(source_review_enabled=False)
    collected_item = CollectedNewsItem(
        source_type="rss",
        source_id="rss-1",
        url="https://example.com/advisory",
        title="Cloud advisory",
        published_at="2026-01-01T00:00:00Z",
        raw_data={"feed_url": "https://example.com/feed.xml", "summary": "technical"},
        full_content="Details",
    )
    collector_service = AsyncMock()
    collector_service.collect_rss_items.return_value = [collected_item]

    insert_mock = AsyncMock(return_value=uuid4())

    monkeypatch.setattr("bcn.workflows.collection.get_pool", AsyncMock())
    monkeypatch.setattr("bcn.workflows.collection.insert_news_item", insert_mock)
    monkeypatch.setattr(
        "bcn.workflows.collection.get_collection_source",
        AsyncMock(return_value={"state": "QUARANTINED"}),
    )
    monkeypatch.setattr(
        "bcn.workflows.collection.collection_source_has_historical_items",
        AsyncMock(return_value=False),
    )

    result = await execute_collection(
        settings,
        source="rss",
        collector_service=collector_service,
        origin="test",
        manage_pool=False,
    )

    assert result == "RSS: collected 0 items"
    insert_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_review_llm_raises_when_json_is_invalid():
    client = Mock()
    client.chat_for_role = AsyncMock(return_value="not-json")
    client.parse_json_response = Mock(side_effect=ValueError("bad json"))
    reviewer = SourceReviewLLM(client)
    sample_item = CollectedNewsItem(
        source_type="rss",
        source_id="rss-1",
        url="https://example.com/advisory",
        title="Cloud advisory",
        published_at="2026-01-01T00:00:00Z",
        raw_data={"feed_url": "https://example.com/feed.xml", "summary": "technical"},
        full_content="Details",
    )

    with pytest.raises(ValueError, match="invalid JSON"):
        await reviewer.review_source(
            source_type="rss",
            display_name="https://example.com/feed.xml",
            raw_config={"feed_url": "https://example.com/feed.xml"},
            sample_items=[sample_item],
        )


@pytest.mark.asyncio
async def test_insert_news_item_parses_rfc822_published_at(monkeypatch):
    from bcn.common.db import insert_news_item

    fetchrow_mock = AsyncMock(return_value={"id": uuid4()})

    class _FakePool:
        def __init__(self):
            self.fetchrow = fetchrow_mock

    monkeypatch.setattr("bcn.common.db.get_pool", AsyncMock(return_value=_FakePool()))

    await insert_news_item(
        source_type="rss",
        source_id="rss-1",
        url="https://example.com/advisory",
        title="Cloud advisory",
        published_at="Fri, 06 Mar 2026 13:00:01 GMT",
        raw_data={"feed_url": "https://example.com/feed.xml"},
        full_content="Details",
    )

    inserted_published_at = fetchrow_mock.await_args.args[5]
    assert inserted_published_at == datetime(
        2026, 3, 6, 13, 0, 1, tzinfo=timezone.utc
    )
    assert fetchrow_mock.await_args.args[8] == "https://example.com/advisory"
    assert fetchrow_mock.await_args.args[9] is None


@pytest.mark.asyncio
async def test_insert_news_item_skips_invalid_published_at(monkeypatch):
    from bcn.common.db import insert_news_item

    fetchrow_mock = AsyncMock(return_value={"id": uuid4()})

    class _FakePool:
        def __init__(self):
            self.fetchrow = fetchrow_mock

    monkeypatch.setattr("bcn.common.db.get_pool", AsyncMock(return_value=_FakePool()))

    inserted = await insert_news_item(
        source_type="rss",
        source_id="rss-invalid",
        url="https://example.com/advisory",
        title="Cloud advisory",
        published_at="definitely-not-a-date",
        raw_data={"feed_url": "https://example.com/feed.xml"},
        full_content="Details",
    )

    assert inserted is None
    fetchrow_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_insert_news_item_skips_empty_published_at(monkeypatch):
    from bcn.common.db import insert_news_item

    fetchrow_mock = AsyncMock(return_value={"id": uuid4()})

    class _FakePool:
        def __init__(self):
            self.fetchrow = fetchrow_mock

    monkeypatch.setattr("bcn.common.db.get_pool", AsyncMock(return_value=_FakePool()))

    inserted = await insert_news_item(
        source_type="rss",
        source_id="rss-empty",
        url="https://example.com/advisory",
        title="Cloud advisory",
        published_at="   ",
        raw_data={"feed_url": "https://example.com/feed.xml"},
        full_content="Details",
    )

    assert inserted is None
    fetchrow_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_insert_news_item_skips_future_published_at(monkeypatch):
    from bcn.common.db import insert_news_item

    fetchrow_mock = AsyncMock(return_value={"id": uuid4()})

    class _FakePool:
        def __init__(self):
            self.fetchrow = fetchrow_mock

    monkeypatch.setattr("bcn.common.db.get_pool", AsyncMock(return_value=_FakePool()))

    inserted = await insert_news_item(
        source_type="rss",
        source_id="rss-future",
        url="https://example.com/advisory",
        title="Cloud advisory",
        published_at="2099-03-06T13:00:01Z",
        raw_data={"feed_url": "https://example.com/feed.xml"},
        full_content="Details",
    )

    assert inserted is None
    fetchrow_mock.assert_not_awaited()
