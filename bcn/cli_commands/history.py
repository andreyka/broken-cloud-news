"""History import CLI commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click

from bcn.cli_commands.shared import build_settings
from bcn.cli_commands.shared import run_async


def register_history_commands(cli: click.Group) -> None:
    """Attach history import commands to the root CLI group."""

    @cli.command("import-history")
    @click.option(
        "--file",
        "file_path",
        type=click.Path(exists=True, dir_okay=False, path_type=Path),
        required=True,
        help="Path to channel export text (Name, [M/D/YYYY H:MM AM] format).",
    )
    @click.option(
        "--channel",
        default="telegram",
        show_default=True,
        help="Distribution channel label for imported history.",
    )
    @click.option(
        "--timezone",
        default="",
        help="Timezone used in the export timestamps (default: BCN_DISTRIBUTE_TIMEZONE).",
    )
    @click.option("--dry-run", is_flag=True, help="Parse and report only, no DB writes.")
    def import_history(
        file_path: Path,
        channel: str,
        timezone: str,
        dry_run: bool,
    ) -> None:
        """Backfill previously published channel posts into DB history."""
        settings = build_settings()

        async def _run() -> None:
            from bcn.history import extract_unique_post_urls
            from bcn.history import parse_channel_history_text
            from bcn.persistence.history import import_channel_history_posts
            from bcn.persistence.runtime import close_pool
            from bcn.persistence.runtime import get_pool

            tz_name = (timezone or settings.distribute_timezone or "UTC").strip()
            try:
                raw_text = file_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise click.ClickException(f"Failed to read {file_path}: {exc}") from exc

            try:
                parsed_posts = parse_channel_history_text(raw_text, timezone_name=tz_name)
            except Exception as exc:
                raise click.ClickException(
                    f"Failed to parse history file with timezone '{tz_name}': {exc}"
                ) from exc

            if not parsed_posts:
                click.echo("No posts found in file.")
                return

            payload: list[dict[str, Any]] = []
            unique_urls: set[str] = set()
            for post in parsed_posts:
                urls = extract_unique_post_urls(post.content_markdown)
                unique_urls.update(urls)
                payload.append(
                    {
                        "author": post.author,
                        "posted_at": post.posted_at,
                        "content_markdown": post.content_markdown,
                        "content_hash": post.content_hash,
                        "urls": urls,
                    }
                )

            earliest = min(post.posted_at for post in parsed_posts)
            latest = max(post.posted_at for post in parsed_posts)
            click.echo(
                "Parsed "
                f"{len(parsed_posts)} posts ({len(unique_urls)} unique URLs), "
                f"range={earliest.isoformat()}..{latest.isoformat()}, tz={tz_name}"
            )

            if dry_run:
                click.echo("Dry-run only; no database changes applied.")
                return

            await get_pool(settings)
            try:
                stats = await import_channel_history_posts(
                    channel=channel,
                    posts=payload,
                )
                click.echo(
                    "History import complete: "
                    f"posts inserted={stats['inserted_posts']}, "
                    f"posts existing={stats['existing_posts']}, "
                    f"urls inserted={stats['inserted_urls']}, "
                    f"urls existing={stats['existing_urls']}, "
                    f"posts skipped={stats['skipped_posts']}"
                )
            finally:
                await close_pool()

        run_async(_run)


__all__ = ["register_history_commands"]
