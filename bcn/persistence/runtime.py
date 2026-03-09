"""Shared asyncpg pool and schema lifecycle for BCN persistence gateways."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from typing import Optional

import asyncpg

from bcn.common.config import Settings
from bcn.common.migrations import apply_migrations
from bcn.common.migrations import get_migration_status

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None
_pool_database_url: str | None = None
_pool_lock: asyncio.Lock = asyncio.Lock()
_schema_ready: bool = False
_schema_lock: asyncio.Lock = asyncio.Lock()


def _pool_not_configured_error() -> RuntimeError:
    """Return the shared explicit-bootstrap error used by persistence gateways."""
    return RuntimeError(
        "Persistence runtime is not configured. Call get_pool(settings) before using "
        "persistence gateways."
    )


async def _get_or_create_pool(settings: Optional[Settings] = None) -> asyncpg.Pool:
    """Return pool instance without enforcing schema migrations."""
    global _pool
    global _pool_database_url
    if _pool is not None:
        if (
            settings is not None
            and _pool_database_url is not None
            and str(settings.database_url) != _pool_database_url
        ):
            raise RuntimeError(
                "Persistence runtime is already configured for a different database_url."
            )
        return _pool
    if settings is None:
        raise _pool_not_configured_error()
    async with _pool_lock:
        if _pool is None:
            _pool = await asyncpg.create_pool(
                settings.database_url,
                min_size=2,
                max_size=10,
            )
            _pool_database_url = str(settings.database_url)
    return _pool


async def ensure_schema_ready(pool: Optional[asyncpg.Pool] = None) -> None:
    """Apply DB migrations once for this process before DB access."""
    global _schema_ready
    if _schema_ready:
        return

    active_pool = pool or _pool
    if active_pool is None:
        raise _pool_not_configured_error()
    async with _schema_lock:
        if _schema_ready:
            return
        applied = await apply_migrations(active_pool)
        if applied:
            logger.info("Applied DB migrations: %s", ", ".join(applied))
        _schema_ready = True


async def migrate_schema(settings: Optional[Settings] = None) -> list[str]:
    """Apply pending schema migrations and mark schema as ready."""
    global _schema_ready
    pool = await _get_or_create_pool(settings)
    applied = await apply_migrations(pool)
    _schema_ready = True
    return applied


async def get_schema_migration_status(
    settings: Optional[Settings] = None,
) -> list[dict[str, Any]]:
    """Return applied/pending migration status rows."""
    pool = await _get_or_create_pool(settings)
    return await get_migration_status(pool)


async def get_pool(settings: Optional[Settings] = None) -> asyncpg.Pool:
    """Return the shared connection pool, creating it on first call."""
    pool = await _get_or_create_pool(settings)
    await ensure_schema_ready(pool=pool)
    return pool


async def close_pool() -> None:
    """Close the shared connection pool if it is open."""
    global _pool
    global _pool_database_url
    global _schema_ready
    async with _pool_lock:
        if _pool is not None:
            await _pool.close()
            _pool = None
            _pool_database_url = None
    _schema_ready = False
