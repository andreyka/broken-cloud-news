"""Persistence gateway for imported historical channel posts."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Any
from uuid import UUID

import asyncpg

from bcn.briefing.story_identity import primary_story_issue_key
from bcn.briefing.story_identity import story_url_key
from bcn.persistence.runtime import ensure_schema_ready
from bcn.persistence.runtime import get_pool


async def ensure_history_tables() -> None:
    """Ensure schema migrations already created history tables."""
    await ensure_schema_ready()


def _history_url_source_id(url: str) -> str:
    """Build stable unique key for history-url synthetic rows."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _history_title(content_markdown: str) -> str:
    """Derive a short title from post text for synthetic URL rows."""
    for raw_line in (content_markdown or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        title = line.lstrip("🔥📉🕵️🦉").strip(" -*")
        return title[:220] if title else "Historical channel post"
    return "Historical channel post"


def _story_identity_values(
    *,
    url: str,
    title: str | None,
    summary: str | None,
) -> tuple[str | None, str | None]:
    """Compute persisted story identity fields for DB-level dedupe."""
    url_key = story_url_key(url or "") or None
    issue_key = primary_story_issue_key(title or "", summary or "") or None
    return url_key, issue_key


async def _upsert_history_url_item(
    conn: asyncpg.Connection,
    *,
    url: str,
    posted_at: datetime,
    channel: str,
    author: str,
    content_hash: str,
    content_markdown: str,
    source_type: str = "history_url",
) -> tuple[UUID, bool]:
    """Upsert one synthetic `news_items` row for a URL present in history."""
    source_id = _history_url_source_id(url)
    title = _history_title(content_markdown)
    payload = {
        "origin": "history_import",
        "channel": channel,
        "author": author,
        "content_hash": content_hash,
        "url": url,
    }
    story_url, story_issue = _story_identity_values(
        url=url,
        title=title,
        summary=None,
    )

    exists = await conn.fetchval(
        """
        SELECT id
        FROM news_items
        WHERE source_type = $1 AND source_id = $2
        LIMIT 1
        """,
        source_type,
        source_id,
    )

    row = await conn.fetchrow(
        """
        INSERT INTO news_items (
            source_type,
            source_id,
            url,
            title,
            published_at,
            raw_data,
            story_url_key,
            story_issue_key,
            status
        )
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, 'PUBLISHED')
        ON CONFLICT (source_type, source_id) DO UPDATE
        SET
            status = 'PUBLISHED',
            published_at = LEAST(news_items.published_at, EXCLUDED.published_at),
            updated_at = NOW(),
            title = COALESCE(news_items.title, EXCLUDED.title),
            raw_data = COALESCE(news_items.raw_data, '{}'::jsonb) || EXCLUDED.raw_data,
            story_url_key = COALESCE(news_items.story_url_key, EXCLUDED.story_url_key),
            story_issue_key = COALESCE(news_items.story_issue_key, EXCLUDED.story_issue_key)
        RETURNING id
        """,
        source_type,
        source_id,
        url,
        title,
        posted_at,
        json.dumps(payload, ensure_ascii=False, default=str),
        story_url,
        story_issue,
    )
    return row["id"], not bool(exists)


async def import_channel_history_posts(
    *,
    channel: str,
    posts: list[dict[str, Any]],
    source_type: str = "history_url",
) -> dict[str, int]:
    """Import previously published channel posts into DB history tables."""
    await ensure_history_tables()
    pool = await get_pool()

    inserted_posts = 0
    existing_posts = 0
    inserted_urls = 0
    existing_urls = 0
    skipped_posts = 0

    async with pool.acquire() as conn:
        async with conn.transaction():
            for post in posts:
                content_hash = str(post.get("content_hash") or "").strip().lower()
                content_markdown = str(post.get("content_markdown") or "").strip()
                posted_at = post.get("posted_at")
                if not content_hash or not content_markdown or not isinstance(
                    posted_at, datetime
                ):
                    skipped_posts += 1
                    continue

                author = str(post.get("author") or "").strip()
                urls = [
                    str(url).strip()
                    for url in (post.get("urls") or [])
                    if str(url).strip()
                ]

                history_exists = await conn.fetchval(
                    """
                    SELECT id
                    FROM published_history_posts
                    WHERE content_hash = $1
                    LIMIT 1
                    """,
                    content_hash,
                )
                if history_exists:
                    existing_posts += 1
                    continue

                item_ids: list[UUID] = []
                for url in urls:
                    item_id, is_new = await _upsert_history_url_item(
                        conn,
                        url=url,
                        posted_at=posted_at,
                        channel=channel,
                        author=author,
                        content_hash=content_hash,
                        content_markdown=content_markdown,
                        source_type=source_type,
                    )
                    item_ids.append(item_id)
                    if is_new:
                        inserted_urls += 1
                    else:
                        existing_urls += 1

                await conn.execute(
                    """
                    INSERT INTO published_history_posts (
                        channel,
                        author,
                        posted_at,
                        content_markdown,
                        content_hash,
                        urls,
                        item_ids,
                        metadata
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, $6::jsonb, $7::uuid[], $8::jsonb
                    )
                    """,
                    channel.strip().lower(),
                    author,
                    posted_at,
                    content_markdown,
                    content_hash,
                    json.dumps(urls, ensure_ascii=False),
                    item_ids,
                    json.dumps({"origin": "history_import"}, ensure_ascii=False),
                )
                inserted_posts += 1

    return {
        "inserted_posts": inserted_posts,
        "existing_posts": existing_posts,
        "inserted_urls": inserted_urls,
        "existing_urls": existing_urls,
        "skipped_posts": skipped_posts,
    }
