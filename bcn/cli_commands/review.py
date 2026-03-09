"""Human review CLI commands."""

from __future__ import annotations

from pathlib import Path

import click

from bcn.cli_commands.shared import build_settings
from bcn.cli_commands.shared import run_async


def register_review_commands(cli: click.Group) -> None:
    """Attach review commands to the root CLI group."""

    @cli.command("review")
    @click.option(
        "--briefing-id", type=str, help="Briefing UUID to review (defaults to latest)."
    )
    @click.option(
        "--decision",
        type=click.Choice(["accept", "reject", "edit", "needs_work"]),
        required=True,
        help="Human review decision label.",
    )
    @click.option(
        "--issue-tag",
        "issue_tags",
        multiple=True,
        help="Issue tag (repeatable).",
    )
    @click.option("--edited-file", type=click.Path(exists=True, dir_okay=False))
    @click.option("--edited-text", type=str, help="Edited markdown text.")
    @click.option("--notes", type=str, help="Free-form reviewer notes.")
    @click.option("--reviewer", type=str, default="cli", show_default=True)
    def review(
        briefing_id: str | None,
        decision: str,
        issue_tags: tuple[str, ...],
        edited_file: str | None,
        edited_text: str | None,
        notes: str | None,
        reviewer: str,
    ) -> None:
        """Store human feedback labels/edits for one briefing."""
        settings = build_settings()

        async def _run() -> None:
            from uuid import UUID

            from bcn.persistence.briefings import get_briefing_by_id
            from bcn.persistence.briefings import get_latest_any_briefing
            from bcn.persistence.runtime import close_pool
            from bcn.persistence.runtime import get_pool
            from bcn.persistence.training import get_latest_generation_run_for_briefing
            from bcn.persistence.training import insert_human_review

            if edited_file and edited_text:
                raise click.ClickException(
                    "Use either --edited-file or --edited-text, not both."
                )

            parsed_id = None
            if briefing_id:
                try:
                    parsed_id = UUID(briefing_id)
                except ValueError as exc:
                    raise click.ClickException(
                        f"Invalid briefing UUID: {briefing_id}"
                    ) from exc

            await get_pool(settings)
            briefing = (
                await get_briefing_by_id(parsed_id)
                if parsed_id
                else await get_latest_any_briefing()
            )
            if not briefing:
                click.echo("No briefing found to review")
                await close_pool()
                return

            edited_markdown = edited_text
            if edited_file:
                edited_markdown = Path(edited_file).read_text(encoding="utf-8")

            run = await get_latest_generation_run_for_briefing(briefing["id"])
            run_id = run["id"] if run else None
            review_id = await insert_human_review(
                briefing_id=briefing["id"],
                run_id=run_id,
                decision=decision,
                issue_tags=list(issue_tags),
                reviewer=reviewer,
                edited_markdown=edited_markdown,
                notes=notes,
            )
            click.echo(
                f"Stored review {review_id} for briefing {briefing['id']} "
                f"(decision={decision}, tags={len(issue_tags)})"
            )
            await close_pool()

        run_async(_run)

    @cli.command("review-queue")
    @click.option("--limit", type=int, default=20, show_default=True)
    @click.option(
        "--only-unreviewed",
        is_flag=True,
        help="Show only briefings without reviews.",
    )
    def review_queue(limit: int, only_unreviewed: bool) -> None:
        """List recent briefings and review status."""
        settings = build_settings()

        async def _run() -> None:
            from bcn.persistence.runtime import close_pool
            from bcn.persistence.runtime import get_pool
            from bcn.persistence.training import get_review_queue

            await get_pool(settings)
            rows = await get_review_queue(
                limit=max(1, int(limit)),
                only_unreviewed=only_unreviewed,
            )
            if not rows:
                click.echo("No briefings in review queue")
                await close_pool()
                return

            for row in rows:
                payload = dict(row)
                click.echo(
                    f"{payload['id']} | status={payload['status']} | reviews={payload['review_count']} "
                    f"| last_decision={payload['last_decision'] or '-'} | created_at={payload['created_at'].isoformat()}"
                )
                preview = str(payload.get("preview") or "").replace("\n", " ")
                if preview:
                    click.echo(f"  preview: {preview[:160]}")
            await close_pool()

        run_async(_run)


__all__ = ["register_review_commands"]
