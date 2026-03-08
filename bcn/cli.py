"""CLI entry-point and daemon mode for Broken Cloud News."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from uuid import UUID

import click

from bcn.common.config import Settings
from bcn.workflows.automation import build_regular_briefing_trigger
from bcn.workflows.automation import build_regular_monthly_newsletter_trigger
from bcn.workflows.analysis import execute_analysis
from bcn.workflows.collection import execute_collection
from bcn.workflows.distribution import execute_distribution
from bcn.workflows.generation import execute_generation as execute_briefing_generation
from bcn.workflows.modes import AD_HOC_MODE
from bcn.workflows.modes import ALL_MODES
from bcn.workflows.modes import REGULAR_DAILY_BRIEFING_MODE
from bcn.workflows.review import execute_critique as critique_briefing
from bcn.workflows.review import execute_verification as verify_briefing
from bcn.workflows.service import execute_workflow_mode
from bcn.workflows.service import run_daemon

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("bcn")

# Backward-compatible aliases kept for tests and external imports.
_build_daily_digest_trigger = build_regular_briefing_trigger
_build_monthly_newsletter_trigger = build_regular_monthly_newsletter_trigger
_WORKFLOW_MODE_CHOICES = click.Choice(list(ALL_MODES), case_sensitive=True)

# ---------------------------------------------------------------------------
# CLI Commands
# ---------------------------------------------------------------------------


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
def cli(verbose: bool) -> None:
    """Broken Cloud News - Cloud Security Briefing Agent."""
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)


@cli.command("db-migrate")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show pending migrations without applying them.",
)
def db_migrate(dry_run: bool) -> None:
    """Apply versioned SQL migrations for BCN database schema."""
    settings = Settings()

    async def _run() -> None:
        from bcn.common.db import close_pool
        from bcn.common.db import get_schema_migration_status
        from bcn.common.db import migrate_schema

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

    asyncio.run(_run())


@cli.command()
@click.option(
    "--source",
    "-s",
    type=click.Choice(["all", "ghsa", "rss", "twitter", "reddit"]),
    default="all",
)
def collect(source: str) -> None:
    """Run collector for all sources or a specific one."""
    settings = Settings()

    async def _run():
        result = await execute_collection(
            settings,
            source=source,
            origin="cli",
            manage_pool=True,
        )
        click.echo(result)

    asyncio.run(_run())


@cli.command()
def analyze() -> None:
    """Score and summarize unprocessed news items via the LLM."""
    settings = Settings()

    async def _run():
        result = await execute_analysis(
            settings,
            source="cli",
            manage_pool=True,
        )
        click.echo(result)

    asyncio.run(_run())


@cli.command()
@click.option(
    "--mode",
    type=_WORKFLOW_MODE_CHOICES,
    default=REGULAR_DAILY_BRIEFING_MODE,
    show_default=True,
    help="Workflow mode for briefing generation.",
)
def write(mode: str) -> None:
    """Generate a briefing with cover image from top-scored items."""
    settings = Settings()

    async def _run():
        result = await execute_briefing_generation(
            settings,
            mode=mode,
            source="cli",
            manage_pool=True,
        )
        click.echo(result)

    asyncio.run(_run())


@cli.command()
@click.option("--latest", is_flag=True, help="Critique the latest stored briefing")
@click.option("--file", "file_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--text", "text_input", type=str, help="Inline markdown text to critique")
def critique(latest: bool, file_path: str | None, text_input: str | None) -> None:
    """Run the critic against latest briefing or provided markdown text."""
    settings = Settings()

    async def _run():
        result = await critique_briefing(
            settings=settings,
            latest=latest,
            file_path=file_path,
            text_input=text_input,
        )
        click.echo(result)

    asyncio.run(_run())


@cli.command()
@click.option("--latest", is_flag=True, help="Verify the latest stored briefing")
@click.option("--file", "file_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--text", "text_input", type=str, help="Inline markdown text to verify")
def verify(latest: bool, file_path: str | None, text_input: str | None) -> None:
    """Run factual verifier against latest briefing or provided markdown text."""
    settings = Settings()

    async def _run():
        result = await verify_briefing(
            settings=settings,
            latest=latest,
            file_path=file_path,
            text_input=text_input,
        )
        click.echo(result)

    asyncio.run(_run())


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
    help="Re-run the Analyst LLM on historical items to capture new logic (e.g. canonical URLs) before Writer simulation.",
)
@click.option(
    "--store-db/--no-store-db",
    default=True,
    show_default=True,
    help="Persist simulation runs/results in PostgreSQL.",
)
def simulate(
    limit: int,
    since_days: int,
    output_path: str,
    include_text: bool,
    with_critic_rewrites: bool,
    reanalyze_items: bool,
    store_db: bool,
) -> None:
    """Simulate historical briefings and compare against actual distributed posts."""
    settings = Settings()

    async def _run():
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

    asyncio.run(_run())


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
    settings = Settings()

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

    asyncio.run(_run())


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
def benchmark(
    cases_path: str,
    candidate_overrides: str | None,
    output_path: str,
    include_text: bool,
    store_db: bool,
) -> None:
    """Run champion and challenger against the benchmark pack."""
    settings = Settings()

    async def _run() -> None:
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

    asyncio.run(_run())


@cli.command("shadow")
@click.option(
    "--mode",
    type=_WORKFLOW_MODE_CHOICES,
    default=REGULAR_DAILY_BRIEFING_MODE,
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
    settings = Settings()

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

    asyncio.run(_run())


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
    settings = Settings()

    async def _run() -> None:
        from bcn.common.db import close_pool
        from bcn.common.db import get_pool
        from bcn.common.db import list_recent_evaluation_runs

        await get_pool(settings)
        rows = await list_recent_evaluation_runs(lane=lane, limit=max(1, int(limit)))
        if not rows:
            click.echo("No evaluation runs found")
            await close_pool()
            return

        for row in rows:
            payload = dict(row)
            summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
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

    asyncio.run(_run())


@cli.command()
@click.option("--briefing-id", type=str, help="Distribute a specific DRAFT briefing UUID.")
@click.option(
    "--mode",
    type=_WORKFLOW_MODE_CHOICES,
    default=REGULAR_DAILY_BRIEFING_MODE,
    show_default=True,
    help="Workflow mode controlling channel policy.",
)
def distribute(briefing_id: str | None, mode: str) -> None:
    """Send the latest briefing to configured distribution channels."""
    settings = Settings()

    async def _run() -> None:
        parsed_briefing_id: UUID | None = None
        if briefing_id:
            try:
                parsed_briefing_id = UUID(briefing_id)
            except ValueError as exc:
                raise click.ClickException(
                    f"Invalid briefing UUID: {briefing_id}"
                ) from exc

        result = await execute_distribution(
            settings,
            mode=mode,
            briefing_id=parsed_briefing_id,
        )
        click.echo(result)

    asyncio.run(_run())


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
    settings = Settings()

    async def _run() -> None:
        from bcn.common.db import close_pool
        from bcn.common.db import get_newsletter_subscribers
        from bcn.common.db import get_pool

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

    asyncio.run(_run())


@newsletter_subscribers.command("add")
@click.argument("email", type=str)
def newsletter_subscribers_add(email: str) -> None:
    """Add or reactivate a newsletter subscriber."""
    settings = Settings()

    async def _run() -> None:
        from bcn.common.db import add_newsletter_subscriber
        from bcn.common.db import close_pool
        from bcn.common.db import get_pool

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

    asyncio.run(_run())


@newsletter_subscribers.command("remove")
@click.argument("email", type=str)
def newsletter_subscribers_remove(email: str) -> None:
    """Deactivate a newsletter subscriber."""
    settings = Settings()

    async def _run() -> None:
        from bcn.common.db import close_pool
        from bcn.common.db import get_pool
        from bcn.common.db import remove_newsletter_subscriber

        await get_pool(settings)
        removed = await remove_newsletter_subscriber(email)
        if removed:
            click.echo(f"Removed newsletter subscriber: {email.strip().lower()}")
        else:
            click.echo(f"Subscriber not found or already inactive: {email.strip().lower()}")
        await close_pool()

    asyncio.run(_run())


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
    "--issue-tag", "issue_tags", multiple=True, help="Issue tag (repeatable)."
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
    settings = Settings()

    async def _run() -> None:
        from uuid import UUID

        from bcn.common.db import close_pool
        from bcn.common.db import get_briefing_by_id
        from bcn.common.db import get_latest_any_briefing
        from bcn.common.db import get_latest_generation_run_for_briefing
        from bcn.common.db import get_pool
        from bcn.common.db import insert_human_review

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
        briefing = None
        if parsed_id:
            briefing = await get_briefing_by_id(parsed_id)
        else:
            briefing = await get_latest_any_briefing()

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

    asyncio.run(_run())


@cli.command("review-queue")
@click.option("--limit", type=int, default=20, show_default=True)
@click.option(
    "--only-unreviewed", is_flag=True, help="Show only briefings without reviews."
)
def review_queue(limit: int, only_unreviewed: bool) -> None:
    """List recent briefings and review status."""
    settings = Settings()

    async def _run() -> None:
        from bcn.common.db import close_pool
        from bcn.common.db import get_pool
        from bcn.common.db import get_review_queue

        await get_pool(settings)
        rows = await get_review_queue(
            limit=max(1, int(limit)), only_unreviewed=only_unreviewed
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

    asyncio.run(_run())


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
    settings = Settings()

    async def _run() -> None:
        from uuid import UUID

        from bcn.common.db import close_pool
        from bcn.common.db import get_pool
        from bcn.common.db import upsert_distribution_outcome

        try:
            parsed_id = UUID(briefing_id)
        except ValueError as exc:
            raise click.ClickException(f"Invalid briefing UUID: {briefing_id}") from exc

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

    asyncio.run(_run())


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
    settings = Settings()

    async def _run() -> None:
        from bcn.common.db import close_pool
        from bcn.common.db import get_pool
        from bcn.common.db import import_channel_history_posts
        from bcn.history import extract_unique_post_urls
        from bcn.history import parse_channel_history_text

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

    asyncio.run(_run())


@cli.command("export-training")
@click.option("--output-dir", default="training_export", show_default=True)
@click.option(
    "--limit",
    type=int,
    default=0,
    show_default=True,
    help="Max runs to export (0=all).",
)
@click.option(
    "--since-days",
    type=int,
    default=0,
    show_default=True,
    help="Only runs from last N days.",
)
@click.option(
    "--include-blocked/--published-only",
    default=False,
    show_default=True,
    help="Include blocked generations in exports.",
)
@click.option(
    "--include-shadow-preferences/--generation-only",
    default=True,
    show_default=True,
    help="Include high-confidence shadow lane preference rows and raw shadow traces.",
)
def export_training(
    output_dir: str,
    limit: int,
    since_days: int,
    include_blocked: bool,
    include_shadow_preferences: bool,
) -> None:
    """Export SFT + preference JSONL datasets from stored traces."""
    settings = Settings()

    async def _run() -> None:
        from datetime import datetime
        from datetime import timezone
        from uuid import UUID

        from bcn.common.db import close_pool
        from bcn.common.db import get_distribution_outcomes
        from bcn.common.db import get_evaluation_runs_for_export
        from bcn.common.db import get_generation_preference_pairs_for_runs
        from bcn.common.db import get_generation_rounds_for_runs
        from bcn.common.db import get_generation_runs_for_export
        from bcn.common.db import get_human_reviews
        from bcn.common.db import get_pool
        from bcn.evaluation import build_shadow_preference_pair

        def _iso(value: Any) -> str | None:
            if isinstance(value, UUID):
                return str(value)
            if hasattr(value, "isoformat"):
                return value.isoformat()
            return str(value) if value is not None else None

        def _json_safe(value: Any) -> Any:
            if isinstance(value, UUID):
                return str(value)
            if hasattr(value, "isoformat"):
                return value.isoformat()
            if isinstance(value, dict):
                return {str(k): _json_safe(v) for k, v in value.items()}
            if isinstance(value, list):
                return [_json_safe(v) for v in value]
            return value

        def _normalize_json(value: Any, default: Any) -> Any:
            if isinstance(value, type(default)):
                return value
            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                except (TypeError, ValueError, json.JSONDecodeError):
                    return default
                if isinstance(parsed, type(default)):
                    return parsed
            return default

        await get_pool(settings)
        runs = await get_generation_runs_for_export(
            limit=max(0, int(limit)),
            since_days=max(0, int(since_days)),
            include_blocked=include_blocked,
        )
        shadow_runs = (
            await get_evaluation_runs_for_export(
                lane="shadow",
                limit=max(0, int(limit)),
                since_days=max(0, int(since_days)),
            )
            if include_shadow_preferences
            else []
        )
        if not runs and not shadow_runs:
            click.echo("No generation runs found for export")
            await close_pool()
            return

        run_ids: list[UUID] = [row["id"] for row in runs]
        briefing_ids: list[UUID] = [
            row["briefing_id"] for row in runs if row["briefing_id"]
        ]
        rounds = await get_generation_rounds_for_runs(run_ids)
        prefs = await get_generation_preference_pairs_for_runs(run_ids)
        reviews = await get_human_reviews(run_ids=run_ids)
        outcomes = (
            await get_distribution_outcomes(briefing_ids=briefing_ids)
            if briefing_ids
            else []
        )

        rounds_by_run: dict[str, list[dict[str, Any]]] = {}
        for row in rounds:
            run_key = str(row["run_id"])
            rounds_by_run.setdefault(run_key, []).append(dict(row))

        reviews_by_run: dict[str, list[dict[str, Any]]] = {}
        reviews_by_briefing: dict[str, list[dict[str, Any]]] = {}
        for row in reviews:
            payload = dict(row)
            run_key = str(payload["run_id"]) if payload.get("run_id") else ""
            briefing_key = (
                str(payload["briefing_id"]) if payload.get("briefing_id") else ""
            )
            if run_key:
                reviews_by_run.setdefault(run_key, []).append(payload)
            if briefing_key:
                reviews_by_briefing.setdefault(briefing_key, []).append(payload)

        outcomes_by_briefing: dict[str, list[dict[str, Any]]] = {}
        for row in outcomes:
            raw_payload = dict(row)
            payload: dict[str, Any] = _json_safe(raw_payload)
            briefing_key = (
                str(payload["briefing_id"]) if payload.get("briefing_id") else ""
            )
            if briefing_key:
                outcomes_by_briefing.setdefault(briefing_key, []).append(payload)

        for payloads in reviews_by_run.values():
            payloads.sort(key=lambda row: row.get("created_at"), reverse=True)
        for payloads in reviews_by_briefing.values():
            payloads.sort(key=lambda row: row.get("created_at"), reverse=True)

        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        sft_path = out_dir / "sft.jsonl"
        pref_path = out_dir / "preference.jsonl"
        trace_path = out_dir / "trace_runs.jsonl"
        shadow_trace_path = out_dir / "shadow_trace.jsonl"
        manifest_path = out_dir / "manifest.json"

        sft_rows: list[dict[str, Any]] = []
        trace_rows: list[dict[str, Any]] = []
        for run in runs:
            run_dict = dict(run)
            run_key = str(run_dict["id"])
            briefing_key = (
                str(run_dict["briefing_id"]) if run_dict.get("briefing_id") else ""
            )
            selected_items = _normalize_json(run_dict.get("selected_items"), [])
            prompts = _normalize_json(run_dict.get("prompts"), {})
            config_snapshot = _normalize_json(run_dict.get("config_snapshot"), {})
            run_reviews = reviews_by_run.get(run_key, [])
            briefing_reviews = reviews_by_briefing.get(briefing_key, [])
            latest_review = (run_reviews or briefing_reviews or [None])[0]

            target_markdown = str(run_dict.get("final_draft") or "").strip()
            if latest_review and latest_review.get("edited_markdown"):
                decision = str(latest_review.get("decision") or "").lower()
                if decision in {"edit", "accept"}:
                    target_markdown = (
                        str(latest_review["edited_markdown"]).strip() or target_markdown
                    )

            if target_markdown:
                sft_rows.append(
                    {
                        "id": run_key,
                        "briefing_id": briefing_key or None,
                        "decision": str(run_dict.get("decision") or ""),
                        "mode": str(run_dict.get("mode") or "standard"),
                        "input": {
                            "selected_items": selected_items,
                            "prompt_versions": prompts,
                        },
                        "output_markdown": target_markdown,
                        "messages": [
                            {
                                "role": "user",
                                "content": json.dumps(
                                    {
                                        "mode": str(run_dict.get("mode") or "standard"),
                                        "selected_items": selected_items,
                                        "prompt_versions": prompts,
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                            {"role": "assistant", "content": target_markdown},
                        ],
                        "metadata": {
                            "created_at": _iso(run_dict.get("created_at")),
                            "rewrite_count": int(run_dict.get("rewrite_count") or 0),
                            "llm_model": run_dict.get("llm_model"),
                            "llm_model_version": run_dict.get("llm_model_version"),
                            "git_sha": run_dict.get("git_sha"),
                            "review_decision": latest_review.get("decision")
                            if latest_review
                            else None,
                            "distribution_outcomes": outcomes_by_briefing.get(
                                briefing_key, []
                            ),
                            "config_snapshot": config_snapshot,
                        },
                    }
                )

            trace_rows.append(
                {
                    "run_id": run_key,
                    "briefing_id": briefing_key or None,
                    "created_at": _iso(run_dict.get("created_at")),
                    "decision": run_dict.get("decision"),
                    "decision_reason": run_dict.get("decision_reason"),
                    "rewrite_count": int(run_dict.get("rewrite_count") or 0),
                    "llm_model": run_dict.get("llm_model"),
                    "llm_model_version": run_dict.get("llm_model_version"),
                    "git_sha": run_dict.get("git_sha"),
                    "selected_items": selected_items,
                    "prompt_versions": prompts,
                    "config_snapshot": config_snapshot,
                    "initial_draft": run_dict.get("initial_draft"),
                    "final_draft": run_dict.get("final_draft"),
                    "final_gate": _normalize_json(run_dict.get("final_gate"), {}),
                    "final_critique": _normalize_json(
                        run_dict.get("final_critique"), {}
                    ),
                    "final_verifier": _normalize_json(
                        run_dict.get("final_verifier"), {}
                    ),
                    "rounds": rounds_by_run.get(run_key, []),
                    "human_reviews": run_reviews or briefing_reviews,
                    "distribution_outcomes": outcomes_by_briefing.get(briefing_key, []),
                }
            )

        pref_rows: list[dict[str, Any]] = []
        run_lookup = {str(dict(run)["id"]): dict(run) for run in runs}
        for row in prefs:
            payload = dict(row)
            run_key = str(payload["run_id"])
            run_context = run_lookup.get(run_key, {})
            pref_rows.append(
                {
                    "id": int(payload["id"]),
                    "run_id": run_key,
                    "source": str(payload.get("source") or "auto_writer_loop"),
                    "round_index": int(payload.get("round_index") or 0),
                    "chosen": str(payload.get("chosen_text") or ""),
                    "rejected": str(payload.get("rejected_text") or ""),
                    "rationale": str(payload.get("rationale") or ""),
                    "context": {
                        "mode": str(run_context.get("mode") or "standard"),
                        "selected_items": _normalize_json(
                            run_context.get("selected_items"), []
                        ),
                        "prompt_versions": _normalize_json(
                            run_context.get("prompts"), {}
                        ),
                    },
                    "metadata": {
                        "created_at": _iso(payload.get("created_at")),
                        "briefing_id": (
                            str(run_context.get("briefing_id"))
                            if run_context.get("briefing_id")
                            else None
                        ),
                    },
                }
            )

        # Add human-edited preference pairs where edits differ from final output.
        for review_list in reviews_by_run.values():
            for review_row in review_list:
                run_id_raw = review_row.get("run_id")
                if not run_id_raw:
                    continue
                run_key = str(run_id_raw)
                run_context = run_lookup.get(run_key, {})
                edited = str(review_row.get("edited_markdown") or "").strip()
                final = str(run_context.get("final_draft") or "").strip()
                if not edited or not final or edited == final:
                    continue
                decision = str(review_row.get("decision") or "").lower()
                if decision not in {"edit", "accept"}:
                    continue
                pref_rows.append(
                    {
                        "id": f"human-{review_row.get('id')}",
                        "run_id": run_key,
                        "source": "human_review",
                        "round_index": -1,
                        "chosen": edited,
                        "rejected": final,
                        "rationale": str(
                            review_row.get("notes") or "human edited preferred variant"
                        ),
                        "context": {
                            "mode": str(run_context.get("mode") or "standard"),
                            "selected_items": _normalize_json(
                                run_context.get("selected_items"), []
                            ),
                            "prompt_versions": _normalize_json(
                                run_context.get("prompts"), {}
                            ),
                        },
                        "metadata": {
                            "review_id": str(review_row.get("id")),
                            "created_at": _iso(review_row.get("created_at")),
                        },
                    }
                )

        shadow_trace_rows: list[dict[str, Any]] = []
        shadow_preference_rows = 0
        for row in shadow_runs:
            row_dict = dict(row)
            report = _normalize_json(row_dict.get("report"), {})
            summary = _normalize_json(row_dict.get("summary"), {})
            if report and "summary" not in report:
                report["summary"] = summary
            trace_row = {
                "shadow_run_id": str(row_dict.get("id")),
                "created_at": _iso(row_dict.get("created_at")),
                "generated_at": _iso(row_dict.get("generated_at")),
                "workflow_mode": str(row_dict.get("workflow_mode") or ""),
                "candidate_overrides": _normalize_json(
                    row_dict.get("candidate_overrides"), {}
                ),
                "summary": summary,
                "report": report,
            }
            shadow_trace_rows.append(trace_row)

            pair = build_shadow_preference_pair(report)
            if not pair:
                continue
            pref_rows.append(
                {
                    "id": f"shadow-{row_dict.get('id')}",
                    "run_id": str(row_dict.get("id")),
                    "source": "shadow_lane",
                    "round_index": 0,
                    "chosen": pair["chosen"],
                    "rejected": pair["rejected"],
                    "rationale": pair["rationale"],
                    "context": {
                        **pair["context"],
                        "candidate_overrides": _normalize_json(
                            row_dict.get("candidate_overrides"), {}
                        ),
                    },
                    "metadata": {
                        "created_at": _iso(row_dict.get("created_at")),
                        "generated_at": _iso(row_dict.get("generated_at")),
                        "workflow_mode": str(row_dict.get("workflow_mode") or ""),
                        "preferred_side": pair["preferred_side"],
                        "recommendation": pair["recommendation"],
                        "confidence": pair["confidence"],
                        "selection_overlap_ratio": pair["selection_overlap_ratio"],
                    },
                }
            )
            shadow_preference_rows += 1

        with sft_path.open("w", encoding="utf-8") as handle:
            for row in sft_rows:
                handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        with pref_path.open("w", encoding="utf-8") as handle:
            for row in pref_rows:
                handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        with trace_path.open("w", encoding="utf-8") as handle:
            for row in trace_rows:
                handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        with shadow_trace_path.open("w", encoding="utf-8") as handle:
            for row in shadow_trace_rows:
                handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

        manifest = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "run_count": len(runs),
            "sft_rows": len(sft_rows),
            "preference_rows": len(pref_rows),
            "trace_rows": len(trace_rows),
            "shadow_trace_rows": len(shadow_trace_rows),
            "shadow_preference_rows": shadow_preference_rows,
            "filters": {
                "limit": int(limit),
                "since_days": int(since_days),
                "include_blocked": bool(include_blocked),
                "include_shadow_preferences": bool(include_shadow_preferences),
            },
            "files": {
                "sft_jsonl": str(sft_path),
                "preference_jsonl": str(pref_path),
                "trace_jsonl": str(trace_path),
                "shadow_trace_jsonl": str(shadow_trace_path),
            },
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        click.echo(
            f"Export complete: runs={len(runs)} sft_rows={len(sft_rows)} "
            f"preference_rows={len(pref_rows)} shadow_preference_rows={shadow_preference_rows}"
        )
        click.echo(f"  SFT: {sft_path}")
        click.echo(f"  Preference: {pref_path}")
        click.echo(f"  Traces: {trace_path}")
        click.echo(f"  Shadow Traces: {shadow_trace_path}")
        click.echo(f"  Manifest: {manifest_path}")
        await close_pool()

    asyncio.run(_run())


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
    settings = Settings()

    async def _run() -> None:
        from bcn.common.db import close_pool
        from bcn.common.db import finalize_stale_pending_generation_runs
        from bcn.common.db import get_pool

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

    asyncio.run(_run())


@cli.command()
@click.option(
    "--mode",
    type=_WORKFLOW_MODE_CHOICES,
    default=REGULAR_DAILY_BRIEFING_MODE,
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
    type=_WORKFLOW_MODE_CHOICES,
    default=AD_HOC_MODE,
    show_default=True,
    help="Workflow mode to execute as a single write->distribute handoff.",
)
def workflow_run(mode: str) -> None:
    """Run one workflow mode cycle without daemon scheduler."""
    settings = Settings()

    async def _run() -> None:
        writer_result, distribute_result = await execute_workflow_mode(
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

    asyncio.run(_run())


@cli.command()
def run() -> None:
    """Start daemon mode with the scheduler."""
    settings = Settings()

    async def _daemon() -> None:
        await run_daemon(
            settings,
            emit=click.echo,
        )

    try:
        asyncio.run(_daemon())
    except KeyboardInterrupt:
        click.echo("\nShutting down...")
