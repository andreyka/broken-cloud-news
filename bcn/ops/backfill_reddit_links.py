"""One-off backfill for Reddit items to prefer useful outbound technical links."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from typing import Any

from bcn.agents.collector.agent import CollectorExecutor
from bcn.common.config import Settings
from bcn.common.db import close_pool
from bcn.common.db import get_pool
from bcn.common.scraper import Scraper

LOGGER = logging.getLogger("backfill_reddit_links")
USER_AGENT = "BrokenCloudNews/1.0 (cloud-security digest bot)"


def _extract_reddit_listing_post_data(payload: Any) -> dict[str, Any]:
    """Extract post metadata from Reddit comments listing payload."""
    if not isinstance(payload, list) or not payload:
        return {}
    listing = payload[0]
    if not isinstance(listing, dict):
        return {}
    children = listing.get("data", {}).get("children", [])
    if not children:
        return {}
    post = children[0]
    if not isinstance(post, dict):
        return {}
    data = post.get("data", {})
    return data if isinstance(data, dict) else {}


async def run_backfill(*, limit: int, dry_run: bool) -> dict[str, Any]:
    """Backfill legacy Reddit rows with technical outbound references."""
    settings = Settings()
    await get_pool(settings)
    pool = await get_pool()
    scraper = Scraper(
        content_limit=settings.scrape_content_limit,
        min_content_length=settings.scrape_min_content_length,
    )

    stats: dict[str, Any] = {
        "scanned": 0,
        "candidates": 0,
        "updated": 0,
        "missing_post_id": 0,
        "fetch_failed": 0,
        "no_useful_outbound": 0,
        "unchanged": 0,
        "dry_run": dry_run,
        "samples": [],
    }

    query = """
        SELECT id, source_id, url, title, raw_data
        FROM news_items
        WHERE source_type = 'reddit'
        ORDER BY published_at DESC
    """
    params: list[Any] = []
    if limit > 0:
        query += " LIMIT $1"
        params.append(limit)

    try:
        rows = await pool.fetch(query, *params)
        stats["scanned"] = len(rows)

        for row in rows:
            current_url = str(row["url"] or "").strip()
            if not current_url or not CollectorExecutor._is_internal_reddit_url(current_url):
                continue
            stats["candidates"] += 1

            source_id = str(row["source_id"] or "").strip()
            raw_data = row["raw_data"]
            raw: dict[str, Any]
            if isinstance(raw_data, dict):
                raw = raw_data
            elif isinstance(raw_data, str):
                try:
                    parsed = json.loads(raw_data)
                except (TypeError, ValueError, json.JSONDecodeError):
                    parsed = {}
                raw = parsed if isinstance(parsed, dict) else {}
            else:
                raw = {}

            permalink = str(raw.get("permalink") or raw.get("link") or current_url).strip()
            post_id = CollectorExecutor._extract_reddit_post_id(source_id, permalink)
            if not post_id:
                stats["missing_post_id"] += 1
                continue

            comments_url = f"https://www.reddit.com/comments/{post_id}/.json"
            try:
                payload_text = await scraper.fetch_text_or_raise(
                    comments_url,
                    headers={"User-Agent": USER_AGENT},
                    timeout_ms=20000,
                )
                payload = json.loads(payload_text)
                post_data = _extract_reddit_listing_post_data(payload)
            except Exception:
                stats["fetch_failed"] += 1
                continue

            metadata = {
                "url_overridden_by_dest": str(
                    post_data.get("url_overridden_by_dest") or ""
                ).strip(),
                "url": str(post_data.get("url") or "").strip(),
            }
            references = CollectorExecutor._extract_reddit_reference_urls(
                permalink,
                metadata,
            )
            title = str(row["title"] or raw.get("title") or "").strip()
            summary = str(raw.get("summary") or "").strip()
            selected_url = CollectorExecutor._select_reddit_primary_url(
                permalink,
                references,
                title=title,
                summary=summary,
            )
            if not selected_url or CollectorExecutor._is_internal_reddit_url(selected_url):
                stats["no_useful_outbound"] += 1
                continue
            if selected_url.rstrip("/") == current_url.rstrip("/"):
                stats["unchanged"] += 1
                continue

            normalized_permalink = CollectorExecutor._normalize_reddit_permalink(
                str(post_data.get("permalink") or permalink)
            )
            patch = {
                "permalink": normalized_permalink,
                "link": normalized_permalink,
                "references": [{"url": ref} for ref in references],
            }

            if not dry_run:
                await pool.execute(
                    """
                    UPDATE news_items
                    SET url = $1,
                        raw_data = COALESCE(raw_data, '{}'::jsonb) || $2::jsonb,
                        updated_at = NOW()
                    WHERE id = $3
                    """,
                    selected_url,
                    json.dumps(patch, ensure_ascii=False),
                    row["id"],
                )
            stats["updated"] += 1
            samples = stats["samples"]
            if isinstance(samples, list) and len(samples) < 5:
                samples.append(f"{post_id}: {current_url} -> {selected_url}")
    finally:
        try:
            await scraper.close()
        finally:
            await close_pool()

    return stats


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill existing Reddit items with useful outbound technical links. "
            "Non-technical outbound links keep Reddit permalinks."
        )
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum number of Reddit rows to scan (0 = all).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and report only; do not write updates.",
    )
    return parser.parse_args()


def main() -> int:
    """CLI entrypoint for module execution."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    args = _parse_args()
    if args.limit < 0:
        raise SystemExit("--limit must be >= 0")

    stats = asyncio.run(run_backfill(limit=int(args.limit), dry_run=bool(args.dry_run)))
    print(
        "Reddit backfill complete: "
        f"scanned={stats['scanned']}, candidates={stats['candidates']}, "
        f"updated={stats['updated']}, missing_post_id={stats['missing_post_id']}, "
        f"fetch_failed={stats['fetch_failed']}, no_useful_outbound={stats['no_useful_outbound']}, "
        f"unchanged={stats['unchanged']}, dry_run={str(stats['dry_run']).lower()}"
    )
    samples = stats.get("samples", [])
    if isinstance(samples, list) and samples:
        print("Sample rewrites:")
        for sample in samples:
            print(f"- {sample}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

