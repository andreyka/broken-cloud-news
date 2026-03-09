"""Persistence gateway for newsletter subscribers."""

from __future__ import annotations

import asyncpg

from bcn.persistence.runtime import ensure_schema_ready
from bcn.persistence.runtime import get_pool


def _normalize_subscriber_email(value: str) -> str:
    """Normalize subscriber email for stable storage/lookup."""
    return str(value or "").strip().lower()


async def ensure_newsletter_tables() -> None:
    """Ensure schema migrations already created newsletter tables."""
    await ensure_schema_ready()


async def add_newsletter_subscriber(email: str) -> bool:
    """Add or reactivate one newsletter subscriber email."""
    normalized = _normalize_subscriber_email(email)
    if not normalized or "@" not in normalized:
        raise ValueError("Invalid email address")
    await ensure_newsletter_tables()
    pool = await get_pool()
    status = await pool.execute(
        """
        INSERT INTO newsletter_subscribers (email, is_active, updated_at)
        VALUES ($1, TRUE, NOW())
        ON CONFLICT (email) DO UPDATE
        SET is_active = TRUE, updated_at = NOW()
        """,
        normalized,
    )
    return status.startswith("INSERT")


async def remove_newsletter_subscriber(email: str) -> bool:
    """Soft-delete one newsletter subscriber by deactivating it."""
    normalized = _normalize_subscriber_email(email)
    if not normalized:
        return False
    await ensure_newsletter_tables()
    pool = await get_pool()
    status = await pool.execute(
        """
        UPDATE newsletter_subscribers
        SET is_active = FALSE, updated_at = NOW()
        WHERE email = $1 AND is_active = TRUE
        """,
        normalized,
    )
    return status == "UPDATE 1"


async def get_newsletter_subscribers(
    *,
    active_only: bool = True,
) -> list[asyncpg.Record]:
    """Return newsletter subscribers ordered by email."""
    await ensure_newsletter_tables()
    pool = await get_pool()
    if active_only:
        return await pool.fetch(
            """
            SELECT id, email, is_active, created_at, updated_at
            FROM newsletter_subscribers
            WHERE is_active = TRUE
            ORDER BY email ASC
            """
        )
    return await pool.fetch(
        """
        SELECT id, email, is_active, created_at, updated_at
        FROM newsletter_subscribers
        ORDER BY email ASC
        """
    )
