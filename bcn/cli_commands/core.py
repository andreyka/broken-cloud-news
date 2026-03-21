"""Core collection, generation, distribution, and daemon CLI commands."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import click

from bcn.common.component_settings import default_service_port
from bcn.cli_commands.shared import build_component_settings
from bcn.cli_commands.shared import build_settings
from bcn.cli_commands.shared import run_async


def register_core_commands(
    cli: click.Group,
    workflow_mode_choices: click.Choice,
    *,
    bindings: Any,
) -> None:
    """Attach the core BCN CLI commands to the root Click group."""

    @cli.command("db-migrate")
    @click.option(
        "--dry-run",
        is_flag=True,
        help="Show pending migrations without applying them.",
    )
    def db_migrate(dry_run: bool) -> None:
        """Apply versioned SQL migrations for BCN database schema."""
        settings = build_settings()

        async def _run() -> None:
            from bcn.persistence.runtime import close_pool
            from bcn.persistence.runtime import get_schema_migration_status
            from bcn.persistence.runtime import migrate_schema

            try:
                if dry_run:
                    rows = await get_schema_migration_status(settings)
                    pending = [row for row in rows if not bool(row.get("applied"))]
                    if not rows:
                        click.echo("No migration files discovered.")
                        return
                    if not pending:
                        click.echo("No pending migrations.")
                        return
                    click.echo(f"Pending migrations: {len(pending)}")
                    for row in pending:
                        click.echo(f"  {row['version']} {row['name']}")
                    return

                applied = await migrate_schema(settings)
                if not applied:
                    click.echo("No pending migrations.")
                else:
                    click.echo(f"Applied migrations: {len(applied)}")
                    for name in applied:
                        click.echo(f"  {name}")

                rows = await get_schema_migration_status(settings)
                applied_count = len([row for row in rows if bool(row.get("applied"))])
                click.echo(f"Schema migration state: {applied_count}/{len(rows)} applied.")
            finally:
                await close_pool()

        run_async(_run)

    @cli.command()
    @click.option(
        "--source",
        "-s",
        type=click.Choice(["all", "ghsa", "rss", "twitter", "reddit"]),
        default="all",
    )
    def collect(source: str) -> None:
        """Run collector for all sources or a specific one."""
        settings = build_settings()

        async def _run() -> None:
            result = await bindings.execute_collection(
                settings,
                source=source,
                origin="cli",
                manage_pool=True,
            )
            click.echo(result)

        run_async(_run)

    @cli.command()
    def analyze() -> None:
        """Score and summarize unprocessed news items via the LLM."""
        settings = build_settings()

        async def _run() -> None:
            result = await bindings.execute_analysis(
                settings,
                source="cli",
                manage_pool=True,
            )
            click.echo(result)

        run_async(_run)

    @cli.command()
    @click.option(
        "--mode",
        type=workflow_mode_choices,
        default="regular_daily_briefing",
        show_default=True,
        help="Workflow mode for briefing generation.",
    )
    def write(mode: str) -> None:
        """Generate a briefing with cover image from top-scored items."""
        settings = build_settings()

        async def _run() -> None:
            result = await bindings.execute_briefing_generation(
                settings,
                mode=mode,
                source="cli",
                manage_pool=True,
            )
            click.echo(result)

        run_async(_run)

    @cli.command()
    @click.option("--latest", is_flag=True, help="Critique the latest stored briefing")
    @click.option("--file", "file_path", type=click.Path(exists=True, dir_okay=False))
    @click.option("--text", "text_input", type=str, help="Inline markdown text to critique")
    def critique(latest: bool, file_path: str | None, text_input: str | None) -> None:
        """Run the critic against latest briefing or provided markdown text."""
        settings = build_settings()

        async def _run() -> None:
            result = await bindings.critique_briefing(
                settings=settings,
                latest=latest,
                file_path=file_path,
                text_input=text_input,
            )
            click.echo(result)

        run_async(_run)

    @cli.command()
    @click.option("--latest", is_flag=True, help="Verify the latest stored briefing")
    @click.option("--file", "file_path", type=click.Path(exists=True, dir_okay=False))
    @click.option("--text", "text_input", type=str, help="Inline markdown text to verify")
    def verify(latest: bool, file_path: str | None, text_input: str | None) -> None:
        """Run factual verifier against latest briefing or provided markdown text."""
        settings = build_settings()

        async def _run() -> None:
            result = await bindings.verify_briefing(
                settings=settings,
                latest=latest,
                file_path=file_path,
                text_input=text_input,
            )
            click.echo(result)

        run_async(_run)

    @cli.command()
    @click.option("--briefing-id", type=str, help="Distribute a specific DRAFT briefing UUID.")
    @click.option(
        "--mode",
        type=workflow_mode_choices,
        default="regular_daily_briefing",
        show_default=True,
        help="Workflow mode controlling channel policy.",
    )
    def distribute(briefing_id: str | None, mode: str) -> None:
        """Send the latest briefing to configured distribution channels."""
        settings = build_settings()

        async def _run() -> None:
            parsed_briefing_id: UUID | None = None
            if briefing_id:
                try:
                    parsed_briefing_id = UUID(briefing_id)
                except ValueError as exc:
                    raise click.ClickException(
                        f"Invalid briefing UUID: {briefing_id}"
                    ) from exc

            result = await bindings.execute_distribution(
                settings,
                mode=mode,
                briefing_id=parsed_briefing_id,
            )
            click.echo(result)

        run_async(_run)

    @cli.command()
    @click.option(
        "--mode",
        type=workflow_mode_choices,
        default="regular_daily_briefing",
        show_default=True,
        help="Workflow mode for write/distribute stages.",
    )
    def pipeline(mode: str) -> None:
        """Run the full pipeline: collect, analyze, write, distribute."""
        ctx = click.get_current_context()

        click.echo("=== COLLECT ===")
        ctx.invoke(collect, source="all")
        click.echo("\n=== ANALYZE ===")
        ctx.invoke(analyze)
        click.echo("\n=== WRITE ===")
        ctx.invoke(write, mode=mode)
        click.echo("\n=== DISTRIBUTE ===")
        ctx.invoke(distribute, mode=mode)
        click.echo("\nPipeline complete.")

    @cli.command("workflow-run")
    @click.option(
        "--mode",
        type=workflow_mode_choices,
        default="ad_hoc",
        show_default=True,
        help="Workflow mode to execute as a single write->distribute handoff.",
    )
    def workflow_run(mode: str) -> None:
        """Run one workflow mode cycle without daemon scheduler."""
        settings = build_settings()

        async def _run() -> None:
            writer_result, distribute_result = await bindings.execute_workflow_mode(
                settings,
                mode=mode,
            )
            click.echo(writer_result)
            if not distribute_result:
                click.echo(
                    "Writer did not return a publish handoff; skipping distribution."
                )
                return

            click.echo(distribute_result)

        run_async(_run)

    @cli.command("serve")
    @click.argument(
        "component",
        type=click.Choice(
            ["writer", "critic", "verifier", "collector", "analyst", "distributor"]
        ),
    )
    @click.option("--host", default="0.0.0.0", show_default=True)
    @click.option(
        "--port",
        type=int,
        default=0,
        show_default=True,
        help="Port to bind (0 selects the component default port).",
    )
    def serve(component: str, host: str, port: int) -> None:
        """Serve one BCN component over JSON/HTTP for remote deployment."""
        settings = build_component_settings(component)
        bind_port = int(port) if int(port) > 0 else default_service_port(component)

        try:
            bindings.serve_component_http(
                settings,
                component=component,
                host=host,
                port=bind_port,
            )
        except KeyboardInterrupt:
            click.echo("\nShutting down...")

    @cli.command()
    def run() -> None:
        """Start the scheduler plus default publish/collection/analysis/eval workers."""
        settings = build_settings()

        async def _daemon() -> None:
            await bindings.run_daemon(
                settings,
                emit=click.echo,
            )

        try:
            run_async(_daemon)
        except KeyboardInterrupt:
            click.echo("\nShutting down...")

    @cli.command("scheduler")
    def scheduler() -> None:
        """Run the enqueue-only scheduler without executing jobs inline."""
        settings = build_settings()

        async def _scheduler() -> None:
            await bindings.run_scheduler(
                settings,
                emit=click.echo,
            )

        try:
            run_async(_scheduler)
        except KeyboardInterrupt:
            click.echo("\nShutting down...")

    @cli.command("worker")
    @click.option(
        "--lane",
        "lanes",
        type=click.Choice(["publish", "collection", "analysis", "evaluation"]),
        multiple=True,
        help="Only process jobs from these lanes (repeatable). Defaults to all lanes.",
    )
    @click.option(
        "--worker-name",
        type=str,
        help="Optional worker label used in queue lease ownership.",
    )
    @click.option(
        "--once",
        is_flag=True,
        help="Process at most one job per selected lane, then exit.",
    )
    def worker(lanes: tuple[str, ...], worker_name: str | None, once: bool) -> None:
        """Run durable workflow workers that lease jobs from the queue."""
        settings = build_settings()

        async def _worker() -> None:
            await bindings.run_worker(
                settings,
                lanes=lanes,
                emit=click.echo,
                worker_name=worker_name,
                once=once,
            )

        try:
            run_async(_worker)
        except KeyboardInterrupt:
            click.echo("\nShutting down...")

    @cli.command("workflow-jobs")
    @click.option(
        "--lane",
        type=click.Choice(["publish", "collection", "analysis", "evaluation"]),
        help="Filter by workflow lane.",
    )
    @click.option(
        "--limit",
        type=int,
        default=20,
        show_default=True,
        help="How many recent jobs to show.",
    )
    def workflow_jobs(lane: str | None, limit: int) -> None:
        """List recent durable workflow jobs."""
        settings = build_settings()

        async def _run() -> None:
            from bcn.persistence.runtime import close_pool
            from bcn.persistence.runtime import get_pool
            from bcn.persistence.workflow_jobs import list_recent_workflow_jobs

            await get_pool(settings)
            rows = await list_recent_workflow_jobs(
                lane=lane,
                limit=max(1, int(limit)),
            )
            if not rows:
                click.echo("No workflow jobs found")
                await close_pool()
                return

            for row in rows:
                payload = dict(row)
                click.echo(
                    f"{payload.get('created_at').isoformat()} | "
                    f"id={payload.get('id')} | "
                    f"lane={payload.get('lane')} | "
                    f"type={payload.get('job_type')} | "
                    f"status={payload.get('status')} | "
                    f"attempts={payload.get('attempt_count')}/{payload.get('max_attempts')} | "
                    f"workflow={payload.get('workflow_id') or '-'}"
                )
            await close_pool()

        run_async(_run)


__all__ = ["register_core_commands"]
