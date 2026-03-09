"""Manual distribution outcome CLI commands."""

from __future__ import annotations

import json
from typing import Any

import click

from bcn.cli_commands.shared import build_settings
from bcn.cli_commands.shared import run_async


def register_distribution_commands(cli: click.Group) -> None:
    """Attach distribution admin commands to the root CLI group."""

    @cli.command("record-outcome")
    @click.option("--briefing-id", required=True, help="Briefing UUID.")
    @click.option(
        "--channel", required=True, help="Channel name (telegram/email/slack/etc)."
    )
    @click.option("--status", default="ok", show_default=True)
    @click.option("--message-id", type=str, help="External message/post id.")
    @click.option("--post-url", type=str, help="External post URL.")
    @click.option("--views", type=int, help="View count metric.")
    @click.option("--reactions", type=int, help="Reaction count metric.")
    @click.option("--clicks", type=int, help="Click count metric.")
    @click.option("--link-clicks", type=str, help="JSON object with per-link clicks.")
    @click.option("--metadata", type=str, help="JSON object with extra metadata.")
    def record_outcome(
        briefing_id: str,
        channel: str,
        status: str,
        message_id: str | None,
        post_url: str | None,
        views: int | None,
        reactions: int | None,
        clicks: int | None,
        link_clicks: str | None,
        metadata: str | None,
    ) -> None:
        """Upsert distribution outcome metrics linked to a briefing."""
        settings = build_settings()

        async def _run() -> None:
            from uuid import UUID

            from bcn.persistence.runtime import close_pool
            from bcn.persistence.runtime import get_pool
            from bcn.persistence.training import upsert_distribution_outcome

            try:
                parsed_id = UUID(briefing_id)
            except ValueError as exc:
                raise click.ClickException(
                    f"Invalid briefing UUID: {briefing_id}"
                ) from exc

            link_clicks_payload: dict[str, Any] = {}
            if link_clicks:
                try:
                    parsed_clicks = json.loads(link_clicks)
                except json.JSONDecodeError as exc:
                    raise click.ClickException(
                        f"--link-clicks must be valid JSON: {exc}"
                    ) from exc
                if isinstance(parsed_clicks, dict):
                    link_clicks_payload = parsed_clicks

            metadata_payload: dict[str, Any] = {}
            if metadata:
                try:
                    parsed_meta = json.loads(metadata)
                except json.JSONDecodeError as exc:
                    raise click.ClickException(
                        f"--metadata must be valid JSON: {exc}"
                    ) from exc
                if isinstance(parsed_meta, dict):
                    metadata_payload = parsed_meta

            metrics: dict[str, Any] = {}
            if views is not None:
                metrics["views"] = int(views)
            if reactions is not None:
                metrics["reactions"] = int(reactions)
            if clicks is not None:
                metrics["clicks"] = int(clicks)
            if link_clicks_payload:
                metrics["link_clicks"] = link_clicks_payload

            await get_pool(settings)
            await upsert_distribution_outcome(
                briefing_id=parsed_id,
                channel=channel,
                status=status,
                external_message_id=message_id,
                external_post_url=post_url,
                metrics=metrics,
                metadata=metadata_payload,
            )
            click.echo(
                f"Stored distribution outcome for briefing {parsed_id} channel={channel} status={status}"
            )
            await close_pool()

        run_async(_run)


__all__ = ["register_distribution_commands"]
