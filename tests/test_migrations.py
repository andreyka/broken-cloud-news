from __future__ import annotations

from datetime import datetime
from datetime import timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


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
    def __init__(self, *, fetch_rows):
        self.execute = AsyncMock()
        self.fetch = AsyncMock(return_value=fetch_rows)

    def transaction(self):
        return _FakeTx()


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquire(self._conn)


def _write_migration(path: Path, name: str, sql: str) -> None:
    path.joinpath(name).write_text(sql, encoding="utf-8")


@pytest.mark.asyncio
async def test_apply_migrations_runs_pending_files(tmp_path: Path):
    from bcn.common.migrations import apply_migrations

    _write_migration(tmp_path, "0001_create_alpha.sql", "CREATE TABLE alpha(id INT);")
    _write_migration(tmp_path, "0002_create_beta.sql", "CREATE TABLE beta(id INT);")

    conn = _FakeConn(fetch_rows=[])
    pool = _FakePool(conn)

    applied = await apply_migrations(pool, migrations_dir=tmp_path)

    assert applied == ["0001_create_alpha.sql", "0002_create_beta.sql"]

    calls = conn.execute.await_args_list
    executed_sql = [str(call.args[0]) for call in calls]
    assert any("pg_advisory_lock" in sql for sql in executed_sql)
    assert any("pg_advisory_unlock" in sql for sql in executed_sql)
    assert any("CREATE TABLE IF NOT EXISTS schema_migrations" in sql for sql in executed_sql)
    assert any("CREATE TABLE alpha" in sql for sql in executed_sql)
    assert any("CREATE TABLE beta" in sql for sql in executed_sql)

    inserts = [
        call
        for call in calls
        if "INSERT INTO schema_migrations" in str(call.args[0])
    ]
    assert len(inserts) == 2
    assert inserts[0].args[1] == "0001"
    assert inserts[1].args[1] == "0002"


@pytest.mark.asyncio
async def test_apply_migrations_raises_on_checksum_mismatch(tmp_path: Path):
    from bcn.common.migrations import apply_migrations

    _write_migration(tmp_path, "0001_create_alpha.sql", "CREATE TABLE alpha(id INT);")

    conn = _FakeConn(fetch_rows=[{"version": "0001", "checksum": "stale-checksum"}])
    pool = _FakePool(conn)

    with pytest.raises(RuntimeError, match="checksum mismatch"):
        await apply_migrations(pool, migrations_dir=tmp_path)

    executed_sql = [str(call.args[0]) for call in conn.execute.await_args_list]
    assert any("pg_advisory_unlock" in sql for sql in executed_sql)


@pytest.mark.asyncio
async def test_get_migration_status_marks_pending(tmp_path: Path):
    from bcn.common.migrations import get_migration_status

    _write_migration(tmp_path, "0001_create_alpha.sql", "CREATE TABLE alpha(id INT);")
    _write_migration(tmp_path, "0002_create_beta.sql", "CREATE TABLE beta(id INT);")

    conn = _FakeConn(
        fetch_rows=[
            {
                "version": "0001",
                "name": "0001_create_alpha.sql",
                "checksum": "abc",
                "applied_at": datetime.now(timezone.utc),
            }
        ]
    )
    pool = _FakePool(conn)

    rows = await get_migration_status(pool, migrations_dir=tmp_path)

    assert len(rows) == 2
    assert rows[0]["version"] == "0001"
    assert rows[0]["applied"] is True
    assert rows[1]["version"] == "0002"
    assert rows[1]["applied"] is False
