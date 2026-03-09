"""Scoped asyncpg runtime and schema lifecycle for BCN persistence gateways."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from dataclasses import field
import logging
from typing import Any
from typing import Optional

import asyncpg

from bcn.common.config import Settings
from bcn.common.migrations import apply_migrations
from bcn.common.migrations import get_migration_status

logger = logging.getLogger(__name__)


@dataclass
class PersistenceRuntime:
    """App-scoped persistence runtime holding one DB pool and schema state."""

    database_url: str
    pool: asyncpg.Pool | None = None
    schema_ready: bool = False
    pool_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    schema_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_current_runtime: ContextVar[PersistenceRuntime | None] = ContextVar(
    "bcn_persistence_runtime",
    default=None,
)


def _pool_not_configured_error() -> RuntimeError:
    """Return the explicit-bootstrap error used by persistence gateways."""
    return RuntimeError(
        "Persistence runtime is not configured. Call get_pool(settings) before using "
        "persistence gateways."
    )


def build_persistence_runtime(settings: Settings) -> PersistenceRuntime:
    """Build one explicit persistence runtime from control-plane settings."""
    return PersistenceRuntime(database_url=str(settings.database_url))


def current_persistence_runtime() -> PersistenceRuntime | None:
    """Return the currently bound persistence runtime, if any."""
    return _current_runtime.get()


def bind_persistence_runtime(settings: Settings) -> PersistenceRuntime:
    """Bind or reuse the current async-context persistence runtime."""
    runtime = current_persistence_runtime()
    database_url = str(settings.database_url)
    if runtime is not None:
        if runtime.database_url != database_url:
            raise RuntimeError(
                "Persistence runtime is already configured for a different database_url."
            )
        return runtime

    runtime = build_persistence_runtime(settings)
    _current_runtime.set(runtime)
    return runtime


def _resolve_runtime(
    *,
    settings: Settings | None = None,
    runtime: PersistenceRuntime | None = None,
) -> PersistenceRuntime:
    """Return the runtime active for the current async context."""
    if runtime is not None:
        if settings is not None and runtime.database_url != str(settings.database_url):
            raise RuntimeError(
                "Persistence runtime does not match the provided database_url."
            )
        return runtime
    if settings is not None:
        return bind_persistence_runtime(settings)

    current = current_persistence_runtime()
    if current is None:
        raise _pool_not_configured_error()
    return current


async def _get_or_create_pool(
    settings: Optional[Settings] = None,
    *,
    runtime: PersistenceRuntime | None = None,
) -> asyncpg.Pool:
    """Return pool instance without enforcing schema migrations."""
    active_runtime = _resolve_runtime(settings=settings, runtime=runtime)
    if active_runtime.pool is not None:
        return active_runtime.pool
    async with active_runtime.pool_lock:
        if active_runtime.pool is None:
            active_runtime.pool = await asyncpg.create_pool(
                active_runtime.database_url,
                min_size=2,
                max_size=10,
            )
    return active_runtime.pool


async def ensure_schema_ready(
    pool: Optional[asyncpg.Pool] = None,
    *,
    runtime: PersistenceRuntime | None = None,
) -> None:
    """Apply DB migrations once for this runtime before DB access."""
    active_runtime = runtime or current_persistence_runtime()
    if active_runtime is None:
        raise _pool_not_configured_error()
    if active_runtime.schema_ready:
        return

    active_pool = pool or active_runtime.pool
    if active_pool is None:
        raise _pool_not_configured_error()
    async with active_runtime.schema_lock:
        if active_runtime.schema_ready:
            return
        applied = await apply_migrations(active_pool)
        if applied:
            logger.info("Applied DB migrations: %s", ", ".join(applied))
        active_runtime.schema_ready = True


async def migrate_schema(
    settings: Optional[Settings] = None,
    *,
    runtime: PersistenceRuntime | None = None,
) -> list[str]:
    """Apply pending schema migrations and mark schema as ready."""
    active_runtime = _resolve_runtime(settings=settings, runtime=runtime)
    pool = await _get_or_create_pool(settings, runtime=active_runtime)
    applied = await apply_migrations(pool)
    active_runtime.schema_ready = True
    return applied


async def get_schema_migration_status(
    settings: Optional[Settings] = None,
    *,
    runtime: PersistenceRuntime | None = None,
) -> list[dict[str, Any]]:
    """Return applied/pending migration status rows."""
    active_runtime = _resolve_runtime(settings=settings, runtime=runtime)
    pool = await _get_or_create_pool(settings, runtime=active_runtime)
    return await get_migration_status(pool)


async def get_pool(
    settings: Optional[Settings] = None,
    *,
    runtime: PersistenceRuntime | None = None,
) -> asyncpg.Pool:
    """Return the current context pool, creating it on first use."""
    active_runtime = _resolve_runtime(settings=settings, runtime=runtime)
    pool = await _get_or_create_pool(settings, runtime=active_runtime)
    await ensure_schema_ready(pool=pool, runtime=active_runtime)
    return pool


async def close_pool(*, runtime: PersistenceRuntime | None = None) -> None:
    """Close the pool bound to the current persistence runtime."""
    active_runtime = runtime or current_persistence_runtime()
    if active_runtime is None:
        return
    async with active_runtime.pool_lock:
        if active_runtime.pool is not None:
            await active_runtime.pool.close()
            active_runtime.pool = None
    active_runtime.schema_ready = False


__all__ = [
    "PersistenceRuntime",
    "bind_persistence_runtime",
    "build_persistence_runtime",
    "close_pool",
    "current_persistence_runtime",
    "ensure_schema_ready",
    "get_pool",
    "get_schema_migration_status",
    "migrate_schema",
]
