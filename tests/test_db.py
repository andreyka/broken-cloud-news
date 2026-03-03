from __future__ import annotations

from unittest.mock import AsyncMock
from unittest.mock import patch
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_get_analyzed_items_excludes_only_distributed_briefings():
    import bcn.common.db as db

    fake_pool = AsyncMock()
    fake_pool.execute = AsyncMock()
    fake_pool.fetch = AsyncMock(return_value=[])
    original_schema_ready = db._schema_ready
    db._schema_ready = True
    try:
        with patch("bcn.common.db.get_pool", new_callable=AsyncMock) as mock_get_pool:
            mock_get_pool.return_value = fake_pool
            await db.get_analyzed_items(
                min_score=8,
                hours=12,
                limit=99,
                stale_writing_minutes=30,
            )
    finally:
        db._schema_ready = original_schema_ready

    args, _kwargs = fake_pool.fetch.await_args
    sql = args[0]

    assert "FROM briefing_items bi" in sql
    assert "JOIN briefings b ON b.id = bi.briefing_id" in sql
    assert "bi.news_item_id = news_items.id" in sql
    assert "b.status = 'DISTRIBUTED'" in sql


class _FakeTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConn:
    def __init__(self, briefing_id):
        self.fetchrow = AsyncMock(return_value={"id": briefing_id})
        self.executemany = AsyncMock()

    def transaction(self):
        return _FakeTx()


class _FakePool:
    def __init__(self, conn):
        self._conn = conn
        self.execute = AsyncMock()

    def acquire(self):
        return _FakeAcquire(self._conn)


@pytest.mark.asyncio
async def test_insert_briefing_writes_join_table_positions():
    import bcn.common.db as db

    briefing_id = uuid4()
    first_item = uuid4()
    second_item = uuid4()

    conn = _FakeConn(briefing_id)
    fake_pool = _FakePool(conn)

    original_schema_ready = db._schema_ready
    db._schema_ready = True
    try:
        with patch("bcn.common.db.get_pool", new_callable=AsyncMock) as mock_get_pool:
            mock_get_pool.return_value = fake_pool
            created = await db.insert_briefing(
                content_markdown="hello",
                content_html=None,
                cover_image_url=None,
                cover_image_prompt=None,
                item_ids=[first_item, second_item, first_item],
            )
    finally:
        db._schema_ready = original_schema_ready

    assert created == briefing_id
    conn.fetchrow.assert_awaited_once()
    conn.executemany.assert_awaited_once()
    args, _kwargs = conn.executemany.await_args
    rows = args[1]
    assert rows == [
        (briefing_id, first_item, 0, "selected"),
        (briefing_id, second_item, 1, "selected"),
    ]


@pytest.mark.asyncio
async def test_upsert_distribution_outcome_appends_attempt_row():
    import bcn.common.db as db

    fake_pool = AsyncMock()
    fake_pool.execute = AsyncMock()
    fake_pool.fetch = AsyncMock(return_value=[])

    original_schema_ready = db._schema_ready
    db._schema_ready = True
    try:
        with patch("bcn.common.db.get_pool", new_callable=AsyncMock) as mock_get_pool:
            mock_get_pool.return_value = fake_pool
            await db.upsert_distribution_outcome(
                briefing_id=uuid4(),
                channel="telegram",
                status="ok",
                metadata={"attempt": 1},
            )
    finally:
        db._schema_ready = original_schema_ready

    args, _kwargs = fake_pool.execute.await_args
    sql = args[0]
    assert "INSERT INTO distribution_attempts" in sql
    assert "ON CONFLICT" not in sql


@pytest.mark.asyncio
async def test_get_distribution_outcomes_reads_latest_view():
    import bcn.common.db as db

    fake_pool = AsyncMock()
    fake_pool.execute = AsyncMock()
    fake_pool.fetch = AsyncMock(return_value=[])

    original_schema_ready = db._schema_ready
    db._schema_ready = True
    try:
        with patch("bcn.common.db.get_pool", new_callable=AsyncMock) as mock_get_pool:
            mock_get_pool.return_value = fake_pool
            await db.get_distribution_outcomes(briefing_ids=[uuid4()], limit=25)
    finally:
        db._schema_ready = original_schema_ready

    args, _kwargs = fake_pool.fetch.await_args
    sql = args[0]
    assert "FROM distribution_outcomes_latest" in sql


@pytest.mark.asyncio
async def test_get_new_items_applies_retry_guards():
    import bcn.common.db as db

    fake_pool = AsyncMock()
    fake_pool.fetch = AsyncMock(return_value=[])

    original_schema_ready = db._schema_ready
    db._schema_ready = True
    try:
        with patch("bcn.common.db.get_pool", new_callable=AsyncMock) as mock_get_pool:
            mock_get_pool.return_value = fake_pool
            await db.get_new_items(
                limit=10,
                stale_analyzing_minutes=15,
                max_analysis_retries=4,
            )
    finally:
        db._schema_ready = original_schema_ready

    args, _kwargs = fake_pool.fetch.await_args
    sql = args[0]
    assert "next_retry_at IS NULL OR next_retry_at <= NOW()" in sql
    assert "terminal_status IS NULL" in sql
    assert "status = 'DISCARDED'" in sql
    assert "COALESCE(retry_count, 0) >= $3" in sql


@pytest.mark.asyncio
async def test_release_items_from_analyzing_records_retry_metadata():
    import bcn.common.db as db

    fake_pool = AsyncMock()
    fake_pool.execute = AsyncMock()

    original_schema_ready = db._schema_ready
    db._schema_ready = True
    try:
        with patch("bcn.common.db.get_pool", new_callable=AsyncMock) as mock_get_pool:
            mock_get_pool.return_value = fake_pool
            item_id = uuid4()
            await db.release_items_from_analyzing(
                [item_id],
                error="analysis timeout",
                max_retries=3,
                base_delay_seconds=30,
                max_delay_seconds=300,
            )
    finally:
        db._schema_ready = original_schema_ready

    args, _kwargs = fake_pool.execute.await_args
    sql = args[0]
    assert "retry_count = COALESCE(retry_count, 0) + 1" in sql
    assert "next_retry_at = CASE" in sql
    assert "terminal_status = CASE" in sql
    assert "status = CASE" in sql
    assert args[2] == "analysis timeout"
    assert args[3] == 3
    assert args[4] == 30
    assert args[5] == 300


@pytest.mark.asyncio
async def test_claim_latest_draft_briefing_applies_retry_guards():
    import bcn.common.db as db

    fake_pool = AsyncMock()
    fake_pool.fetchrow = AsyncMock(return_value=None)

    original_schema_ready = db._schema_ready
    db._schema_ready = True
    try:
        with patch("bcn.common.db.get_pool", new_callable=AsyncMock) as mock_get_pool:
            mock_get_pool.return_value = fake_pool
            await db.claim_latest_draft_briefing(
                stale_distributing_minutes=20,
                max_distribution_retries=5,
            )
    finally:
        db._schema_ready = original_schema_ready

    args, _kwargs = fake_pool.fetchrow.await_args
    sql = args[0]
    assert "status = 'FAILED'" in sql
    assert "next_retry_at IS NULL OR next_retry_at <= NOW()" in sql
    assert "terminal_status IS NULL" in sql
    assert "COALESCE(retry_count, 0) >= $2" in sql


@pytest.mark.asyncio
async def test_release_briefing_for_retry_records_retry_metadata():
    import bcn.common.db as db

    fake_pool = AsyncMock()
    fake_pool.execute = AsyncMock()

    original_schema_ready = db._schema_ready
    db._schema_ready = True
    try:
        with patch("bcn.common.db.get_pool", new_callable=AsyncMock) as mock_get_pool:
            mock_get_pool.return_value = fake_pool
            briefing_id = uuid4()
            await db.release_briefing_for_retry(
                briefing_id,
                error="telegram failed",
                max_retries=4,
                base_delay_seconds=45,
                max_delay_seconds=900,
            )
    finally:
        db._schema_ready = original_schema_ready

    args, _kwargs = fake_pool.execute.await_args
    sql = args[0]
    assert "retry_count = COALESCE(retry_count, 0) + 1" in sql
    assert "status = CASE" in sql
    assert "THEN 'FAILED'" in sql
    assert "next_retry_at = CASE" in sql
    assert args[2] == "telegram failed"
    assert args[3] == 4
    assert args[4] == 45
    assert args[5] == 900
