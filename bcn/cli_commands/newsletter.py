"""Newsletter subscriber CLI commands."""

from __future__ import annotations

import click

from bcn.cli_commands.shared import build_settings
from bcn.cli_commands.shared import run_async


def register_newsletter_commands(cli: click.Group) -> None:
    """Attach newsletter subscriber commands to the root CLI group."""

    @cli.group("newsletter-subscribers")
    def newsletter_subscribers() -> None:
        """Manage monthly newsletter email subscribers."""

    @newsletter_subscribers.command("list")
    @click.option(
        "--all",
        "include_inactive",
        is_flag=True,
        help="Include inactive subscribers.",
    )
    def newsletter_subscribers_list(include_inactive: bool) -> None:
        """List newsletter subscribers from the database."""
        settings = build_settings()

        async def _run() -> None:
            from bcn.persistence.newsletter import get_newsletter_subscribers
            from bcn.persistence.runtime import close_pool
            from bcn.persistence.runtime import get_pool

            await get_pool(settings)
            rows = await get_newsletter_subscribers(active_only=not include_inactive)
            if not rows:
                click.echo("No newsletter subscribers found")
                await close_pool()
                return

            for row in rows:
                payload = dict(row)
                status = "active" if payload.get("is_active") else "inactive"
                click.echo(
                    f"{payload.get('email')} | status={status} "
                    f"| updated_at={payload.get('updated_at').isoformat()}"
                )
            await close_pool()

        run_async(_run)

    @newsletter_subscribers.command("add")
    @click.argument("email", type=str)
    def newsletter_subscribers_add(email: str) -> None:
        """Add or reactivate a newsletter subscriber."""
        settings = build_settings()

        async def _run() -> None:
            from bcn.persistence.newsletter import add_newsletter_subscriber
            from bcn.persistence.runtime import close_pool
            from bcn.persistence.runtime import get_pool

            await get_pool(settings)
            try:
                inserted = await add_newsletter_subscriber(email)
            except ValueError as exc:
                raise click.ClickException(str(exc)) from exc
            click.echo(
                f"{'Added' if inserted else 'Reactivated'} newsletter subscriber: "
                f"{email.strip().lower()}"
            )
            await close_pool()

        run_async(_run)

    @newsletter_subscribers.command("remove")
    @click.argument("email", type=str)
    def newsletter_subscribers_remove(email: str) -> None:
        """Deactivate a newsletter subscriber."""
        settings = build_settings()

        async def _run() -> None:
            from bcn.persistence.newsletter import remove_newsletter_subscriber
            from bcn.persistence.runtime import close_pool
            from bcn.persistence.runtime import get_pool

            await get_pool(settings)
            removed = await remove_newsletter_subscriber(email)
            if removed:
                click.echo(f"Removed newsletter subscriber: {email.strip().lower()}")
            else:
                click.echo(
                    f"Subscriber not found or already inactive: {email.strip().lower()}"
                )
            await close_pool()

        run_async(_run)


__all__ = ["register_newsletter_commands"]
