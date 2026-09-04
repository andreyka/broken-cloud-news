"""Weekly flagship review CLI: list held drafts and approve for distribution."""

from __future__ import annotations

from uuid import UUID

import click

from bcn.cli_commands.shared import build_settings
from bcn.cli_commands.shared import run_async


def register_flagship_commands(cli: click.Group) -> None:
    """Attach weekly flagship review commands to the root CLI group."""

    @cli.command("flagship-pending")
    def flagship_pending() -> None:
        """List weekly flagship drafts awaiting review."""
        settings = build_settings()

        async def _run() -> None:
            from bcn.persistence.briefings import get_briefings_by_status
            from bcn.persistence.runtime import close_pool
            from bcn.persistence.runtime import get_pool
            from bcn.workflows.modes.weekly_flagship import AWAITING_REVIEW_STATUS

            await get_pool(settings)
            rows = await get_briefings_by_status(AWAITING_REVIEW_STATUS)
            if not rows:
                click.echo("No flagship drafts awaiting review.")
            for row in rows:
                title = str(row["title"] or "(untitled)")
                click.echo(f"{row['id']}  {row['created_at']}  {title}")
            await close_pool()

        run_async(_run)

    @cli.command("flagship-approve")
    @click.option(
        "--briefing-id",
        default="",
        help="Draft to approve; defaults to the newest one awaiting review.",
    )
    def flagship_approve(briefing_id: str) -> None:
        """Approve a held flagship draft and distribute it."""
        settings = build_settings()

        async def _run() -> None:
            from bcn.contracts.modes import WEEKLY_FLAGSHIP_MODE
            from bcn.persistence.briefings import get_briefing_by_id
            from bcn.persistence.briefings import get_briefings_by_status
            from bcn.persistence.briefings import set_briefing_status
            from bcn.persistence.runtime import close_pool
            from bcn.persistence.runtime import get_pool
            from bcn.workflows.distribution import execute_distribution
            from bcn.workflows.modes.weekly_flagship import AWAITING_REVIEW_STATUS

            await get_pool(settings)
            try:
                rows = await get_briefings_by_status(AWAITING_REVIEW_STATUS)
                pending_ids = {str(row["id"]) for row in rows}
                target = briefing_id.strip() or (str(rows[0]["id"]) if rows else "")
                if not target or target not in pending_ids:
                    click.echo(
                        "No matching draft awaiting review. "
                        "Run `bcn flagship-pending` to list them."
                    )
                    return

                target_uuid = UUID(target)
                await set_briefing_status(target_uuid, "DRAFT")
                message = ""
                try:
                    message = await execute_distribution(
                        settings,
                        mode=WEEKLY_FLAGSHIP_MODE,
                        briefing_id=target_uuid,
                        manage_pool=False,
                    )
                finally:
                    # Anything short of DISTRIBUTED goes back to the review
                    # queue: a stranded DRAFT would be invisible to this
                    # command and claimable by the daily distributor.
                    row = await get_briefing_by_id(target_uuid)
                    status = str(dict(row).get("status") or "") if row else ""
                    if status != "DISTRIBUTED":
                        await set_briefing_status(target_uuid, AWAITING_REVIEW_STATUS)
                        click.echo(
                            f"Distribution did not complete (status={status or 'unknown'}); "
                            "draft returned to the review queue. "
                            f"{message}".strip()
                        )
                if status == "DISTRIBUTED":
                    click.echo(f"Approved and distributed {target}: {message}")
            finally:
                await close_pool()

        run_async(_run)


__all__ = ["register_flagship_commands"]
