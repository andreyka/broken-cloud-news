"""Offline optimization CLI commands."""

from __future__ import annotations

import click

from bcn.cli_commands.shared import build_settings
from bcn.cli_commands.shared import run_async


def register_optimization_commands(cli: click.Group) -> None:
    """Attach optimization commands to the root CLI group."""

    @cli.command("optimize-run")
    @click.option(
        "--variant",
        "variant_path",
        type=click.Path(exists=True, dir_okay=False),
        required=True,
        help="Variant JSON file defining settings/prompt overrides.",
    )
    @click.option(
        "--benchmark-pack",
        "benchmark_pack_path",
        type=click.Path(exists=True, dir_okay=False),
        help="Optional benchmark pack JSON. If omitted, an auto-built pack is used.",
    )
    @click.option("--replay-limit", type=int, default=20, show_default=True)
    @click.option("--replay-since-days", type=int, default=60, show_default=True)
    @click.option("--benchmark-since-days", type=int, default=90, show_default=True)
    @click.option(
        "--output-dir",
        default="optimization_runs",
        show_default=True,
        help="Directory where run artifacts should be written.",
    )
    @click.option("--store-db/--no-store-db", default=True, show_default=True)
    def optimize_run(
        variant_path: str,
        benchmark_pack_path: str | None,
        replay_limit: int,
        replay_since_days: int,
        benchmark_since_days: int,
        output_dir: str,
        store_db: bool,
    ) -> None:
        """Run one offline optimization comparison against the current champion."""
        settings = build_settings()

        async def _run() -> None:
            from bcn.optimization import execute_optimization_run

            report = await execute_optimization_run(
                settings,
                variant_path=variant_path,
                benchmark_pack_path=benchmark_pack_path,
                replay_limit=max(0, int(replay_limit)),
                replay_since_days=max(0, int(replay_since_days)),
                benchmark_since_days=max(0, int(benchmark_since_days)),
                output_dir=output_dir,
                store_db=store_db,
            )
            summary = report.get("summary", {}) if isinstance(report, dict) else {}
            click.echo(
                f"Optimization variant={report.get('variant', {}).get('id', '')} "
                f"recommendation={summary.get('recommendation', 'hold')} "
                f"score={summary.get('composite_score', 0)}"
            )
            reasons = summary.get("hard_reject_reasons", [])
            if reasons:
                click.echo("Hard reject reasons: " + ", ".join(str(item) for item in reasons))
            click.echo(f"Artifacts: {report.get('output_dir')}")
            if store_db:
                click.echo(f"DB run id: {report.get('db_run_id')}")
                click.echo(f"DB candidate id: {report.get('db_candidate_id')}")

        run_async(_run)

    @cli.command("optimize-runs")
    @click.option("--limit", type=int, default=20, show_default=True)
    def optimize_runs(limit: int) -> None:
        """List recent persisted optimization runs."""
        settings = build_settings()

        async def _run() -> None:
            from bcn.persistence.optimization import list_recent_optimization_runs
            from bcn.persistence.runtime import close_pool
            from bcn.persistence.runtime import get_pool

            await get_pool(settings)
            try:
                rows = await list_recent_optimization_runs(limit=max(1, int(limit)))
                if not rows:
                    click.echo("No optimization runs found")
                    return
                for row in rows:
                    click.echo(
                        f"{row['created_at']}  {row['id']}  status={row['status']}  "
                        f"variant={row['variant_id'] or ''}  "
                        f"recommendation={row['recommendation'] or ''}  "
                        f"score={row['composite_score'] if row['composite_score'] is not None else ''}  "
                        f"hard_reject={bool(row['hard_reject'])}"
                    )
            finally:
                await close_pool()

        run_async(_run)
