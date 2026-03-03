"""Versioned SQL migration runner for BCN schema lifecycle."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
from typing import Any

import asyncpg

_MIGRATION_LOCK_KEY = 86113579
_MIGRATION_NAME_RE = re.compile(r"^(?P<version>\d{4,})_[a-z0-9_]+\.sql$")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def resolve_migrations_dir() -> Path:
    """Resolve SQL migration directory from env/configured defaults."""
    env_dir = os.getenv("BCN_MIGRATIONS_DIR", "").strip()
    candidates: list[Path] = []
    if env_dir:
        candidates.append(Path(env_dir))

    candidates.extend(
        [
            _repo_root() / "postgres" / "migrations",
            Path.cwd() / "postgres" / "migrations",
        ]
    )

    for candidate in candidates:
        if candidate.is_dir():
            return candidate

    joined = ", ".join(str(p) for p in candidates)
    raise FileNotFoundError(
        f"Could not find migrations directory. Checked: {joined}"
    )


def _discover_migrations(migrations_dir: Path) -> list[tuple[str, Path]]:
    migrations: list[tuple[str, Path]] = []
    versions_seen: set[str] = set()

    for path in sorted(migrations_dir.glob("*.sql")):
        match = _MIGRATION_NAME_RE.match(path.name)
        if match is None:
            continue
        version = match.group("version")
        if version in versions_seen:
            raise RuntimeError(f"Duplicate migration version detected: {version}")
        versions_seen.add(version)
        migrations.append((version, path))

    migrations.sort(key=lambda item: item[0])
    return migrations


def _checksum(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


async def _ensure_tracking_table(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version VARCHAR(64) PRIMARY KEY,
            name TEXT NOT NULL,
            checksum VARCHAR(64) NOT NULL,
            applied_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
        """
    )


async def apply_migrations(
    pool: asyncpg.Pool,
    *,
    migrations_dir: Path | None = None,
) -> list[str]:
    """Apply pending SQL migrations in ascending version order."""
    directory = migrations_dir or resolve_migrations_dir()
    migrations = _discover_migrations(directory)
    if not migrations:
        return []

    applied_now: list[str] = []

    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock($1)", _MIGRATION_LOCK_KEY)
        try:
            await _ensure_tracking_table(conn)
            rows = await conn.fetch(
                """
                SELECT version, checksum
                FROM schema_migrations
                """
            )
            applied: dict[str, str] = {
                str(row["version"]): str(row["checksum"]) for row in rows
            }

            for version, path in migrations:
                sql = path.read_text(encoding="utf-8")
                if not sql.strip():
                    raise RuntimeError(f"Migration {path.name} is empty")

                checksum = _checksum(sql)
                existing_checksum = applied.get(version)
                if existing_checksum:
                    if existing_checksum != checksum:
                        raise RuntimeError(
                            f"Migration checksum mismatch for {path.name} "
                            f"(version {version})."
                        )
                    continue

                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        """
                        INSERT INTO schema_migrations (version, name, checksum)
                        VALUES ($1, $2, $3)
                        """,
                        version,
                        path.name,
                        checksum,
                    )
                applied_now.append(path.name)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _MIGRATION_LOCK_KEY)

    return applied_now


async def get_migration_status(
    pool: asyncpg.Pool,
    *,
    migrations_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Return migration status with applied/pending markers."""
    directory = migrations_dir or resolve_migrations_dir()
    migrations = _discover_migrations(directory)

    async with pool.acquire() as conn:
        await _ensure_tracking_table(conn)
        rows = await conn.fetch(
            """
            SELECT version, name, checksum, applied_at
            FROM schema_migrations
            """
        )

    applied_map: dict[str, dict[str, Any]] = {
        str(row["version"]): {
            "name": str(row["name"]),
            "checksum": str(row["checksum"]),
            "applied_at": row["applied_at"],
        }
        for row in rows
    }

    status: list[dict[str, Any]] = []
    for version, path in migrations:
        applied = applied_map.get(version)
        status.append(
            {
                "version": version,
                "name": path.name,
                "applied": applied is not None,
                "applied_at": applied["applied_at"] if applied else None,
            }
        )
    return status
