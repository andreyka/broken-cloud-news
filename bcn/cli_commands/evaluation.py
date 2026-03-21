"""Evaluation and benchmarking CLI commands."""

from __future__ import annotations

import click

from bcn.cli_commands.shared import build_settings
from bcn.cli_commands.shared import run_async


def register_evaluation_commands(cli: click.Group, workflow_mode_choices: click.Choice) -> None:
    """Attach evaluation-related CLI commands to the root Click group."""

    @cli.command()
    @click.option(
        "--limit",
        type=int,
        default=0,
        show_default=True,
        help="How many distributed briefings to simulate (0 = all).",
    )
    @click.option(
        "--since-days",
        type=int,
        default=0,
        show_default=True,
        help="Only simulate briefings from the last N days (0 = all time).",
    )
    @click.option(
        "--output",
        "output_path",
        type=click.Path(dir_okay=False),
        default="simulation_report.json",
        show_default=True,
        help="Where to write the simulation comparison report (JSON).",
    )
    @click.option(
        "--include-text",
        is_flag=True,
        help="Include full actual/simulated markdown in the JSON report.",
    )
    @click.option(
        "--with-critic-rewrites",
        is_flag=True,
        help="Use full writer->critic rewrite loop during simulation (much slower).",
    )
    @click.option(
        "--reanalyze-items",
        is_flag=True,
        help="Re-run the Analyst LLM on historical items to capture new logic before Writer simulation.",
    )
    @click.option(
        "--store-db/--no-store-db",
        default=True,
        show_default=True,
        help="Persist simulation runs/results in PostgreSQL.",
    )
    @click.option(
        "--enqueue/--no-enqueue",
        default=False,
        show_default=True,
        help="Queue the replay onto an evaluation worker instead of running inline.",
    )
    def simulate(
        limit: int,
        since_days: int,
        output_path: str,
        include_text: bool,
        with_critic_rewrites: bool,
        reanalyze_items: bool,
        store_db: bool,
        enqueue: bool,
    ) -> None:
        """Simulate historical briefings and compare against actual distributed posts."""
        settings = build_settings()

        async def _run() -> None:
            if enqueue:
                if not store_db:
                    raise click.ClickException(
                        "Queued simulation runs require --store-db so they can checkpoint and resume."
                    )
                from bcn.workflows.queue import enqueue_simulation_job

                job_id = await enqueue_simulation_job(
                    settings,
                    limit=max(0, int(limit)),
                    since_days=max(0, int(since_days)),
                    output_path=output_path,
                    include_text=include_text,
                    with_critic_rewrites=with_critic_rewrites,
                    reanalyze_items=reanalyze_items,
                    source="cli",
                )
                click.echo(f"Queued simulation job: {job_id}")
                click.echo("Run `bcn worker --lane evaluation` to process it.")
                return

            from bcn.evaluation.service import execute_simulation_lane

            report = await execute_simulation_lane(
                settings,
                limit=max(0, int(limit)),
                since_days=max(0, int(since_days)),
                output_path=output_path,
                include_text=include_text,
                with_critic_rewrites=with_critic_rewrites,
                reanalyze_items=reanalyze_items,
                store_db=store_db,
            )

            summary = report.get("summary", {}) if isinstance(report, dict) else {}
            click.echo(
                "Simulation complete: "
                f"count={report.get('count', 0)} "
                f"avg_actual={summary.get('avg_actual_score', 0)} "
                f"avg_simulated={summary.get('avg_simulated_score', 0)} "
                f"avg_delta={summary.get('avg_delta', 0)}"
            )
            click.echo(
                "Outcome split: "
                f"improved={summary.get('improved', 0)} "
                f"regressed={summary.get('regressed', 0)} "
                f"equal={summary.get('equal', 0)}"
            )
            gate_quality = (
                summary.get("gate_quality", {}) if isinstance(summary, dict) else {}
            )
            if isinstance(gate_quality, dict):
                click.echo(
                    "Hard-gate pass rate: "
                    f"actual={gate_quality.get('actual_hard_pass_rate', 0)} "
                    f"simulated={gate_quality.get('simulated_hard_pass_rate', 0)} "
                    f"change={gate_quality.get('hard_pass_rate_change', 0)}"
                )
            focus_metrics = (
                summary.get("focus_metrics", {}) if isinstance(summary, dict) else {}
            )
            if isinstance(focus_metrics, dict):
                click.echo(
                    "Human-writer pass rate: "
                    f"actual={focus_metrics.get('human_writer_pass_rate_actual', 0)} "
                    f"simulated={focus_metrics.get('human_writer_pass_rate_simulated', 0)} "
                    f"change={focus_metrics.get('human_writer_pass_rate_change', 0)}"
                )
                click.echo(
                    "Formatting-clean pass rate: "
                    f"actual={focus_metrics.get('formatting_clean_pass_rate_actual', 0)} "
                    f"simulated={focus_metrics.get('formatting_clean_pass_rate_simulated', 0)} "
                    f"change={focus_metrics.get('formatting_clean_pass_rate_change', 0)}"
                )
                click.echo(
                    "Duplicate-link issue rate: "
                    f"actual={focus_metrics.get('duplicate_link_issue_rate_actual', 0)} "
                    f"simulated={focus_metrics.get('duplicate_link_issue_rate_simulated', 0)} "
                    f"change={focus_metrics.get('duplicate_link_issue_rate_change', 0)}"
                )
            decision = summary.get("decision", {}) if isinstance(summary, dict) else {}
            if isinstance(decision, dict) and decision:
                click.echo(
                    "Recommendation: "
                    f"{decision.get('recommendation', 'hold')} "
                    f"(confidence={decision.get('confidence', 'low')})"
                )
                rationale = str(decision.get("rationale", "") or "").strip()
                if rationale:
                    click.echo(f"Decision rationale: {rationale}")
            if store_db:
                click.echo(f"DB run id: {report.get('db_run_id')}")
                comparison = report.get("comparison_to_previous_run")
                if isinstance(comparison, dict):
                    click.echo(
                        "Compared with previous run: "
                        f"overlap={comparison.get('overlap_count', 0)} "
                        f"avg_sim_score_change={comparison.get('avg_simulated_score_change', 0)} "
                        f"improved={comparison.get('improved_vs_previous', 0)} "
                        f"regressed={comparison.get('regressed_vs_previous', 0)}"
                    )
                    click.echo(
                        "Decision shift: "
                        f"{comparison.get('baseline_decision', '')} -> {comparison.get('current_decision', '')} "
                        f"(changed={comparison.get('decision_changed', False)})"
                    )
                    click.echo(
                        "Quality-focus shift: "
                        f"human_writer={comparison.get('human_writer_pass_rate_change', 0)} "
                        f"formatting_clean={comparison.get('formatting_clean_pass_rate_change', 0)} "
                        f"dup_link_issue={comparison.get('duplicate_link_issue_rate_change', 0)}"
                    )
                else:
                    click.echo("No previous simulation run available for comparison.")
            click.echo(f"Report written to {output_path}")
            click.echo("No distribution action was performed.")

        run_async(_run)

    @cli.command("benchmark-pack")
    @click.option(
        "--limit",
        type=int,
        default=50,
        show_default=True,
        help="How many benchmark cases to export (0 = all matching cases).",
    )
    @click.option(
        "--since-days",
        type=int,
        default=90,
        show_default=True,
        help="Only include runs from the last N days.",
    )
    @click.option(
        "--output",
        "output_path",
        type=click.Path(dir_okay=False),
        default="benchmark_pack.json",
        show_default=True,
        help="Where to write the benchmark pack JSON.",
    )
    @click.option(
        "--include-unreviewed",
        is_flag=True,
        help="Include unreviewed published runs as fallback accept cases.",
    )
    @click.option(
        "--include-nonpublishable",
        is_flag=True,
        help="Include reviewed reject/needs_work cases as informational benchmark rows.",
    )
    def benchmark_pack(
        limit: int,
        since_days: int,
        output_path: str,
        include_unreviewed: bool,
        include_nonpublishable: bool,
    ) -> None:
        """Build a curated benchmark pack from stored runs and reviews."""
        settings = build_settings()

        async def _run() -> None:
            from bcn.evaluation.service import build_benchmark_pack_artifact

            pack = await build_benchmark_pack_artifact(
                settings,
                limit=max(0, int(limit)),
                since_days=max(0, int(since_days)),
                include_unreviewed=include_unreviewed,
                include_nonpublishable=include_nonpublishable,
                output_path=output_path,
            )
            click.echo(
                f"Benchmark pack written to {output_path} (cases={pack.get('count', 0)})"
            )

        run_async(_run)

    @cli.command("benchmark")
    @click.option(
        "--cases",
        "cases_path",
        type=click.Path(exists=True, dir_okay=False),
        required=True,
        help="Benchmark pack JSON created by `bcn benchmark-pack`.",
    )
    @click.option(
        "--candidate-overrides",
        type=click.Path(exists=True, dir_okay=False),
        help="JSON file with challenger Settings overrides.",
    )
    @click.option(
        "--output",
        "output_path",
        type=click.Path(dir_okay=False),
        default="benchmark_report.json",
        show_default=True,
        help="Where to write the benchmark report JSON.",
    )
    @click.option(
        "--include-text",
        is_flag=True,
        help="Include selected items and history context in the JSON report.",
    )
    @click.option(
        "--store-db/--no-store-db",
        default=True,
        show_default=True,
        help="Persist benchmark runs in PostgreSQL.",
    )
    @click.option(
        "--enqueue/--no-enqueue",
        default=False,
        show_default=True,
        help="Queue the benchmark onto an evaluation worker instead of running inline.",
    )
    def benchmark(
        cases_path: str,
        candidate_overrides: str | None,
        output_path: str,
        include_text: bool,
        store_db: bool,
        enqueue: bool,
    ) -> None:
        """Run champion and challenger against the benchmark pack."""
        settings = build_settings()

        async def _run() -> None:
            if enqueue:
                if not store_db:
                    raise click.ClickException(
                        "Queued benchmark runs require --store-db so they can checkpoint and resume."
                    )
                from bcn.workflows.queue import enqueue_benchmark_job

                job_id = await enqueue_benchmark_job(
                    settings,
                    cases_path=cases_path,
                    candidate_overrides_path=candidate_overrides,
                    output_path=output_path,
                    include_text=include_text,
                    source="cli",
                )
                click.echo(f"Queued benchmark job: {job_id}")
                click.echo("Run `bcn worker --lane evaluation` to process it.")
                return

            from bcn.evaluation.service import execute_benchmark_lane

            report = await execute_benchmark_lane(
                settings,
                cases_path=cases_path,
                candidate_overrides_path=candidate_overrides,
                output_path=output_path,
                include_text=include_text,
                store_db=store_db,
            )
            summary = report.get("summary", {}) if isinstance(report, dict) else {}
            click.echo(
                "Benchmark complete: "
                f"count={report.get('count', 0)} "
                f"champion_pass={summary.get('champion_case_pass_rate', 0)} "
                f"candidate_pass={summary.get('candidate_case_pass_rate', 0)}"
            )
            click.echo(
                "Recommendation: "
                f"{summary.get('recommendation', 'hold')} "
                f"(confidence={summary.get('confidence', 'low')})"
            )
            if store_db:
                click.echo(f"DB run id: {report.get('db_run_id')}")
            click.echo(f"Report written to {output_path}")

        run_async(_run)

    @cli.command("shadow")
    @click.option(
        "--mode",
        type=workflow_mode_choices,
        default="regular_daily_briefing",
        show_default=True,
        help="Workflow mode to evaluate in shadow.",
    )
    @click.option(
        "--candidate-overrides",
        type=click.Path(exists=True, dir_okay=False),
        help="JSON file with challenger Settings overrides.",
    )
    @click.option(
        "--output",
        "output_path",
        type=click.Path(dir_okay=False),
        default="shadow_report.json",
        show_default=True,
        help="Where to write the shadow report JSON.",
    )
    @click.option(
        "--include-text",
        is_flag=True,
        help="Include full generated markdown in the report.",
    )
    @click.option(
        "--store-db/--no-store-db",
        default=True,
        show_default=True,
        help="Persist shadow runs in PostgreSQL.",
    )
    def shadow(
        mode: str,
        candidate_overrides: str | None,
        output_path: str,
        include_text: bool,
        store_db: bool,
    ) -> None:
        """Compare champion and challenger on current upcoming items without publishing."""
        settings = build_settings()

        async def _run() -> None:
            from bcn.evaluation.service import execute_shadow_lane

            report = await execute_shadow_lane(
                settings,
                workflow_mode=mode,
                candidate_overrides_path=candidate_overrides,
                output_path=output_path,
                include_text=include_text,
                store_db=store_db,
            )
            summary = report.get("summary", {}) if isinstance(report, dict) else {}
            click.echo(
                "Shadow evaluation complete: "
                f"mode={mode} "
                f"item_pool={report.get('item_pool_count', 0)} "
                f"selection_overlap={summary.get('selection_overlap_ratio', 0)}"
            )
            click.echo(
                "Recommendation: "
                f"{summary.get('recommendation', 'hold')} "
                f"(confidence={summary.get('confidence', 'low')})"
            )
            if store_db:
                click.echo(f"DB run id: {report.get('db_run_id')}")
            click.echo(f"Report written to {output_path}")

        run_async(_run)

    @cli.command("evaluation-runs")
    @click.option(
        "--lane",
        type=click.Choice(["benchmark", "shadow"]),
        help="Filter by evaluation lane.",
    )
    @click.option(
        "--limit",
        type=int,
        default=10,
        show_default=True,
        help="How many recent runs to show.",
    )
    def evaluation_runs(lane: str | None, limit: int) -> None:
        """List recent stored benchmark and shadow runs."""
        settings = build_settings()

        async def _run() -> None:
            from bcn.persistence.evaluation import list_recent_evaluation_runs
            from bcn.persistence.runtime import close_pool
            from bcn.persistence.runtime import get_pool

            await get_pool(settings)
            rows = await list_recent_evaluation_runs(
                lane=lane,
                limit=max(1, int(limit)),
            )
            if not rows:
                click.echo("No evaluation runs found")
                await close_pool()
                return

            for row in rows:
                payload = dict(row)
                summary = (
                    payload.get("summary")
                    if isinstance(payload.get("summary"), dict)
                    else {}
                )
                click.echo(
                    f"{payload.get('created_at').isoformat()} | "
                    f"lane={payload.get('lane')} | "
                    f"id={payload.get('id')} | "
                    f"status={payload.get('status', 'completed')} | "
                    f"recommendation={summary.get('recommendation', 'hold')} | "
                    f"confidence={summary.get('confidence', 'low')} | "
                    f"count={payload.get('count', 0)}"
                )
            await close_pool()

        run_async(_run)


__all__ = ["register_evaluation_commands"]
