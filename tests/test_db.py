from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import AsyncMock
from unittest.mock import patch
from uuid import uuid4

import pytest


@contextmanager
def _schema_ready_runtime():
    import bcn.persistence.runtime as runtime

    original_schema_ready = runtime._schema_ready
    runtime._schema_ready = True
    try:
        yield
    finally:
        runtime._schema_ready = original_schema_ready


@pytest.mark.asyncio
async def test_get_analyzed_items_excludes_items_already_in_live_briefings():
    import bcn.common.db as db

    fake_pool = AsyncMock()
    fake_pool.execute = AsyncMock()
    fake_pool.fetch = AsyncMock(return_value=[])
    with _schema_ready_runtime(), patch(
        "bcn.persistence.news_items.get_pool",
        new_callable=AsyncMock,
    ) as mock_get_pool:
        mock_get_pool.return_value = fake_pool
        await db.get_analyzed_items(
            min_score=8,
            hours=12,
            limit=99,
            stale_writing_minutes=30,
        )

    args, _kwargs = fake_pool.fetch.await_args
    sql = args[0]

    assert "FROM briefing_items bi" in sql
    assert "JOIN briefings b ON b.id = bi.briefing_id" in sql
    assert "JOIN news_items published_item ON published_item.id = bi.news_item_id" in sql
    assert "b.status IN ('DRAFT', 'DISTRIBUTING', 'DISTRIBUTED')" in sql
    assert "story_issue_key" in sql
    assert "story_url_key" in sql
    assert "ROW_NUMBER() OVER" in sql


@pytest.mark.asyncio
async def test_preview_analyzed_items_is_read_only():
    import bcn.common.db as db

    fake_pool = AsyncMock()
    fake_pool.fetch = AsyncMock(return_value=[])

    with _schema_ready_runtime(), patch(
        "bcn.persistence.news_items.get_pool",
        new_callable=AsyncMock,
    ) as mock_get_pool:
        mock_get_pool.return_value = fake_pool
        await db.preview_analyzed_items(min_score=7, hours=24, limit=15)

    args, _kwargs = fake_pool.fetch.await_args
    sql = args[0]
    assert "WITH ranked AS" in sql
    assert "story_rank = 1" in sql
    assert "status = 'ANALYZED'" in sql
    assert "UPDATE news_items" not in sql
    assert "b.status IN ('DRAFT', 'DISTRIBUTING', 'DISTRIBUTED')" in sql


@pytest.mark.asyncio
async def test_get_top_items_for_period_dedupes_by_story_identity():
    import bcn.common.db as db

    fake_pool = AsyncMock()
    fake_pool.fetch = AsyncMock(return_value=[])

    with _schema_ready_runtime(), patch(
        "bcn.persistence.news_items.get_pool",
        new_callable=AsyncMock,
    ) as mock_get_pool:
        mock_get_pool.return_value = fake_pool
        await db.get_top_items_for_period(days=31, min_score=7, limit=12)

    args, _kwargs = fake_pool.fetch.await_args
    sql = args[0]
    assert "WITH ranked AS" in sql
    assert "story_issue_key" in sql
    assert "story_url_key" in sql
    assert "story_rank = 1" in sql
    assert "b.status IN ('DRAFT', 'DISTRIBUTING')" in sql


@pytest.mark.asyncio
async def test_get_recent_published_items_uses_distribution_time_for_novelty():
    import bcn.common.db as db

    fake_pool = AsyncMock()
    fake_pool.fetch = AsyncMock(return_value=[])

    with _schema_ready_runtime(), patch(
        "bcn.persistence.news_items.get_pool",
        new_callable=AsyncMock,
    ) as mock_get_pool, patch(
        "bcn.persistence.news_items._backfill_recent_story_identity",
        new_callable=AsyncMock,
    ):
        mock_get_pool.return_value = fake_pool
        await db.get_recent_published_items(hours=48, limit=9)

    args, _kwargs = fake_pool.fetch.await_args
    sql = args[0]
    assert "FROM briefing_items bi" in sql
    assert "JOIN briefings b ON b.id = bi.briefing_id" in sql
    assert "JOIN news_items n ON n.id = bi.news_item_id" in sql
    assert "b.status = 'DISTRIBUTED'" in sql
    assert "b.distributed_at > NOW()" in sql
    assert "ORDER BY b.distributed_at DESC" in sql


@pytest.mark.asyncio
async def test_backfill_recent_story_identity_does_not_touch_updated_at():
    import bcn.common.db as db

    fake_pool = AsyncMock()
    fake_pool.fetch = AsyncMock(
        return_value=[
            {
                "id": uuid4(),
                "url": "https://example.com/advisory",
                "title": "Cloud advisory",
                "summary": "GHSA-ab12-cd34-ef56 affects kube auth.",
            }
        ]
    )
    fake_pool.executemany = AsyncMock()

    with patch("bcn.persistence.news_items.get_pool", new_callable=AsyncMock) as mock_get_pool:
        mock_get_pool.return_value = fake_pool
        await db._backfill_recent_story_identity(limit=10, lookback_days=30)

    fetch_sql = fake_pool.fetch.await_args.args[0]
    assert "story_url_key IS NULL" in fetch_sql
    assert "~*" in fetch_sql

    update_sql = fake_pool.executemany.await_args.args[0]
    assert "story_url_key = COALESCE($1, story_url_key)" in update_sql
    assert "story_issue_key = COALESCE($2, story_issue_key)" in update_sql
    assert "updated_at = NOW()" not in update_sql


@pytest.mark.asyncio
async def test_insert_evaluation_report_stores_lane_and_report():
    import bcn.persistence.evaluation as evaluation_db

    fake_pool = AsyncMock()
    run_id = uuid4()
    fake_pool.fetchrow = AsyncMock(return_value={"id": run_id})

    with _schema_ready_runtime(), patch(
        "bcn.persistence.evaluation.get_pool",
        new_callable=AsyncMock,
    ) as mock_get_pool:
        mock_get_pool.return_value = fake_pool
        created = await evaluation_db.insert_evaluation_report(
            {
                "generated_at": "2026-03-06T12:00:00+00:00",
                "lane": "benchmark",
                "pack_path": "/tmp/benchmark_pack.json",
                "count": 7,
                "summary": {"recommendation": "hold"},
                "results": [],
            },
            report_path="/tmp/benchmark_report.json",
        )

    assert created == run_id
    args, _kwargs = fake_pool.fetchrow.await_args
    sql = args[0]
    assert "INSERT INTO evaluation_runs" in sql
    assert args[2] == "benchmark"
    assert args[5] == "/tmp/benchmark_pack.json"
    assert args[11] == 7


@pytest.mark.asyncio
async def test_create_evaluation_run_starts_in_running_state():
    import bcn.persistence.evaluation as evaluation_db

    fake_pool = AsyncMock()
    run_id = uuid4()
    fake_pool.fetchrow = AsyncMock(return_value={"id": run_id})

    with _schema_ready_runtime(), patch(
        "bcn.persistence.evaluation.get_pool",
        new_callable=AsyncMock,
    ) as mock_get_pool:
        mock_get_pool.return_value = fake_pool
        created = await evaluation_db.create_evaluation_run(
            lane="shadow",
            source="scheduler",
            workflow_mode="regular_daily_briefing",
        )

    assert created == run_id
    args, _kwargs = fake_pool.fetchrow.await_args
    sql = args[0]
    assert "INSERT INTO evaluation_runs" in sql
    assert "'running'" in sql
    assert args[1] == "shadow"
    assert args[2] == "scheduler"


@pytest.mark.asyncio
async def test_complete_evaluation_run_updates_status_and_report():
    import bcn.persistence.evaluation as evaluation_db

    fake_pool = AsyncMock()
    fake_pool.execute = AsyncMock()

    with _schema_ready_runtime(), patch(
        "bcn.persistence.evaluation.get_pool",
        new_callable=AsyncMock,
    ) as mock_get_pool:
        mock_get_pool.return_value = fake_pool
        await evaluation_db.complete_evaluation_run(
            uuid4(),
            {
                "generated_at": "2026-03-06T12:00:00+00:00",
                "lane": "shadow",
                "workflow_mode": "regular_daily_briefing",
                "item_pool_count": 9,
                "summary": {"recommendation": "hold"},
                "candidate_overrides": {"llm_model_writer": "candidate"},
                "champion": {},
                "candidate": {},
            },
            report_path="/tmp/shadow_report.json",
        )

    args, _kwargs = fake_pool.execute.await_args
    sql = args[0]
    assert "UPDATE evaluation_runs" in sql
    assert "status = 'completed'" in sql
    assert args[3] == "/tmp/shadow_report.json"


@pytest.mark.asyncio
async def test_fail_evaluation_run_marks_row_failed():
    import bcn.persistence.evaluation as evaluation_db

    fake_pool = AsyncMock()
    fake_pool.execute = AsyncMock()

    with _schema_ready_runtime(), patch(
        "bcn.persistence.evaluation.get_pool",
        new_callable=AsyncMock,
    ) as mock_get_pool:
        mock_get_pool.return_value = fake_pool
        await evaluation_db.fail_evaluation_run(uuid4(), error_message="boom")

    args, _kwargs = fake_pool.execute.await_args
    sql = args[0]
    assert "status = 'failed'" in sql
    assert args[5] == "boom"


@pytest.mark.asyncio
async def test_list_recent_evaluation_runs_applies_lane_filter():
    import bcn.persistence.evaluation as evaluation_db

    fake_pool = AsyncMock()
    fake_pool.fetch = AsyncMock(return_value=[])

    with _schema_ready_runtime(), patch(
        "bcn.persistence.evaluation.get_pool",
        new_callable=AsyncMock,
    ) as mock_get_pool:
        mock_get_pool.return_value = fake_pool
        await evaluation_db.list_recent_evaluation_runs(lane="shadow", limit=5)

    args, _kwargs = fake_pool.fetch.await_args
    sql = args[0]
    assert "FROM evaluation_runs" in sql
    assert "WHERE lane = $1" in sql


@pytest.mark.asyncio
async def test_get_evaluation_runs_for_export_reads_full_shadow_reports():
    import bcn.persistence.evaluation as evaluation_db

    fake_pool = AsyncMock()
    fake_pool.fetch = AsyncMock(return_value=[])

    with _schema_ready_runtime(), patch(
        "bcn.persistence.evaluation.get_pool",
        new_callable=AsyncMock,
    ) as mock_get_pool:
        mock_get_pool.return_value = fake_pool
        await evaluation_db.get_evaluation_runs_for_export(
            lane="shadow",
            since_days=14,
            limit=7,
        )

    args, _kwargs = fake_pool.fetch.await_args
    sql = args[0]
    assert "FROM evaluation_runs" in sql
    assert "candidate_overrides" in sql
    assert "summary" in sql
    assert "report" in sql
    assert "lane = $1" in sql
    assert "status = 'completed'" in sql


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

    with _schema_ready_runtime(), patch(
        "bcn.persistence.briefings.get_pool",
        new_callable=AsyncMock,
    ) as mock_get_pool:
        mock_get_pool.return_value = fake_pool
        created = await db.insert_briefing(
            content_markdown="hello",
            content_html=None,
            cover_image_url=None,
            cover_image_prompt=None,
            item_ids=[first_item, second_item, first_item],
        )

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

    with _schema_ready_runtime(), patch(
        "bcn.persistence.training.get_pool",
        new_callable=AsyncMock,
    ) as mock_get_pool:
        mock_get_pool.return_value = fake_pool
        await db.upsert_distribution_outcome(
            briefing_id=uuid4(),
            channel="telegram",
            status="ok",
            metadata={"attempt": 1},
        )

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

    with _schema_ready_runtime(), patch(
        "bcn.persistence.training.get_pool",
        new_callable=AsyncMock,
    ) as mock_get_pool:
        mock_get_pool.return_value = fake_pool
        await db.get_distribution_outcomes(briefing_ids=[uuid4()], limit=25)

    args, _kwargs = fake_pool.fetch.await_args
    sql = args[0]
    assert "FROM distribution_outcomes_latest" in sql


@pytest.mark.asyncio
async def test_get_new_items_applies_retry_guards():
    import bcn.common.db as db

    fake_pool = AsyncMock()
    fake_pool.fetch = AsyncMock(return_value=[])

    with _schema_ready_runtime(), patch(
        "bcn.persistence.news_items.get_pool",
        new_callable=AsyncMock,
    ) as mock_get_pool:
        mock_get_pool.return_value = fake_pool
        await db.get_new_items(
            limit=10,
            stale_analyzing_minutes=15,
            max_analysis_retries=4,
        )

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

    with _schema_ready_runtime(), patch(
        "bcn.persistence.news_items.get_pool",
        new_callable=AsyncMock,
    ) as mock_get_pool:
        mock_get_pool.return_value = fake_pool
        item_id = uuid4()
        await db.release_items_from_analyzing(
            [item_id],
            error="analysis timeout",
            max_retries=3,
            base_delay_seconds=30,
            max_delay_seconds=300,
        )

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

    with _schema_ready_runtime(), patch(
        "bcn.persistence.briefings.get_pool",
        new_callable=AsyncMock,
    ) as mock_get_pool:
        mock_get_pool.return_value = fake_pool
        await db.claim_latest_draft_briefing(
            stale_distributing_minutes=20,
            max_distribution_retries=5,
        )

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

    with _schema_ready_runtime(), patch(
        "bcn.persistence.briefings.get_pool",
        new_callable=AsyncMock,
    ) as mock_get_pool:
        mock_get_pool.return_value = fake_pool
        briefing_id = uuid4()
        await db.release_briefing_for_retry(
            briefing_id,
            error="telegram failed",
            max_retries=4,
            base_delay_seconds=45,
            max_delay_seconds=900,
        )

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
