"""Recovery and maintenance CLI commands."""

from __future__ import annotations

import click

from bcn.cli_commands.shared import build_settings
from bcn.cli_commands.shared import run_async


def register_recovery_commands(cli: click.Group) -> None:
    """Attach recovery commands to the root CLI group."""

    @cli.command("finalize-pending-runs")
    @click.option(
        "--max-age-minutes",
        type=int,
        default=180,
        show_default=True,
        help="Only finalize PENDING generation runs older than this threshold.",
    )
    @click.option(
        "--decision",
        type=click.Choice(["blocked", "skipped"]),
        default="blocked",
        show_default=True,
        help="Decision label to set on stale PENDING runs.",
    )
    def finalize_pending_runs(max_age_minutes: int, decision: str) -> None:
        """Finalize stale PENDING generation runs to avoid dangling traces."""
        settings = build_settings()

        async def _run() -> None:
            from bcn.persistence.runtime import close_pool
            from bcn.persistence.runtime import get_pool
            from bcn.persistence.training import finalize_stale_pending_generation_runs

            await get_pool(settings)
            updated = await finalize_stale_pending_generation_runs(
                max_age_minutes=max(1, int(max_age_minutes)),
                decision=decision.upper(),
                decision_reason=f"manual_finalize_stale_pending_run:{decision.lower()}",
            )
            click.echo(
                f"Finalized {updated} stale PENDING generation runs as {decision.upper()}"
            )
            await close_pool()

        run_async(_run)


__all__ = ["register_recovery_commands"]
