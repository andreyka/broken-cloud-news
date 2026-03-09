"""Shared helper utilities for BCN persistence gateways."""

from __future__ import annotations

from uuid import UUID

_RETRY_ERROR_MAX_LEN = 512


def briefing_item_ids_sql(alias: str = "b") -> str:
    """Return item-id expression derived from briefing_items join rows."""
    return f"""
        COALESCE(
            ARRAY(
                SELECT bi.news_item_id
                FROM briefing_items bi
                WHERE bi.briefing_id = {alias}.id
                ORDER BY bi.position ASC, bi.created_at ASC
            ),
            '{{}}'::uuid[]
        )
    """


def dedupe_item_ids(item_ids: list[UUID]) -> list[UUID]:
    """De-duplicate item ids while preserving order."""
    deduped: list[UUID] = []
    seen: set[UUID] = set()
    for item_id in item_ids:
        if item_id in seen:
            continue
        seen.add(item_id)
        deduped.append(item_id)
    return deduped


def normalize_retry_policy(
    *,
    max_retries: int,
    base_delay_seconds: int,
    max_delay_seconds: int,
) -> tuple[int, int, int]:
    """Return safe retry policy values used in SQL backoff calculations."""
    retries = max(1, int(max_retries))
    base_delay = max(1, int(base_delay_seconds))
    max_delay = max(base_delay, int(max_delay_seconds))
    return retries, base_delay, max_delay


def normalize_retry_error(error: str | None, *, fallback: str) -> str:
    """Normalize and truncate retry error text for stable DB storage."""
    value = str(error or "").strip()
    if not value:
        value = fallback
    return value[:_RETRY_ERROR_MAX_LEN]
