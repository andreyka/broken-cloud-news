"""CLI entry-point and daemon mode for Broken Cloud News."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import re
from typing import Any
from uuid import UUID
from uuid import uuid4

import click
import httpx

from bcn.common.config import Settings
from bcn.workflows.automation import build_regular_briefing_trigger
from bcn.workflows.automation import build_regular_monthly_newsletter_trigger
from bcn.workflows.automation import configure_scheduler_runtime
from bcn.workflows.automation import job_analyze_items
from bcn.workflows.automation import job_collect_ghsa
from bcn.workflows.automation import job_collect_reddit
from bcn.workflows.automation import job_collect_rss
from bcn.workflows.automation import job_collect_twitter
from bcn.workflows.automation import (
    job_publish_regular_briefing as _job_daily_digest,
)
from bcn.workflows.automation import (
    job_publish_regular_monthly_newsletter as _job_monthly_newsletter,
)
from bcn.workflows.modes import AD_HOC_MODE
from bcn.workflows.modes import ALL_MODES
from bcn.workflows.modes import REGULAR_DAILY_BRIEFING_MODE
from bcn.workflows.modes import REGULAR_MONTHLY_NEWSLETTER_MODE
from bcn.workflows.modes.common import run_writer_distributor_handoff

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("bcn")

# Backward-compatible aliases kept for tests and external imports.
_build_daily_digest_trigger = build_regular_briefing_trigger
_build_monthly_newsletter_trigger = build_regular_monthly_newsletter_trigger
_WORKFLOW_MODE_CHOICES = click.Choice(list(ALL_MODES), case_sensitive=True)
_REDDIT_COMMENT_ID_RE = re.compile(
    r"/comments/([a-z0-9]+)/",
    flags=re.IGNORECASE,
)


def _is_internal_reddit_url(url: str) -> bool:
    """Return whether URL points to Reddit-owned domains."""
    try:
        host = httpx.URL(url).host or ""
    except Exception:
        return False
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host in {
        "reddit.com",
        "old.reddit.com",
        "new.reddit.com",
        "np.reddit.com",
        "redd.it",
        "i.redd.it",
        "v.redd.it",
        "redditmedia.com",
    }


def _extract_reddit_post_id(source_id: str, url: str) -> str:
    """Extract Reddit post id from source id or permalink URL."""
    sid = (source_id or "").strip()
    if sid.startswith("t3_"):
        return sid[3:]
    match = _REDDIT_COMMENT_ID_RE.search(url or "")
    if match:
        return match.group(1).lower()
    return ""


def _normalize_reddit_permalink(permalink: str) -> str:
    """Normalize Reddit permalink into absolute URL form."""
    text = (permalink or "").strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://")):
        return text
    if text.startswith("/"):
        return f"https://www.reddit.com{text}"
    return f"https://www.reddit.com/{text}"


def _extract_reddit_listing_post_data(payload: Any) -> dict[str, Any]:
    """Extract post-level data from Reddit comments listing JSON."""
    if not isinstance(payload, list) or not payload:
        return {}
    listing = payload[0]
    if not isinstance(listing, dict):
        return {}
    children = listing.get("data", {}).get("children", [])
    if not children:
        return {}
    post = children[0]
    if not isinstance(post, dict):
        return {}
    data = post.get("data", {})
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# A2A client helpers
# ---------------------------------------------------------------------------
async def _send_to_agent(
    port: int, skill: str, *, timeout_seconds: int = 180
) -> str:
    """Send a JSON-RPC message to a local A2A agent and return its reply.

    Args:
        port: TCP port the target agent is listening on.
        skill: The skill/command string to send.

    Returns:
        The text content extracted from the agent's response.
    """
    from a2a.client import A2AClient
    from a2a.types import Message
    from a2a.types import MessageSendParams
    from a2a.types import SendMessageRequest
    from a2a.types import TextPart

    async with httpx.AsyncClient(timeout=timeout_seconds) as http_client:
        client = A2AClient(http_client, url=f"http://localhost:{port}")

        message = Message(
            role="user",
            parts=[TextPart(text=skill)],
            message_id=uuid4().hex,
        )
        request = SendMessageRequest(
            id=uuid4().hex,
            params=MessageSendParams(message=message),
        )
        response = await client.send_message(request)

        # Extract text from response
        result = response.model_dump(mode="json", exclude_none=True)
        try:
            artifacts = result.get("result", {}).get("artifacts", [])
            if artifacts:
                parts = artifacts[0].get("parts", [])
                if parts:
                    return parts[0].get("text", str(result))
            # Try message path
            msg = result.get("result", {}).get("status", {})
            return str(msg) if msg else str(result)
        except (KeyError, IndexError):
            return str(result)


async def _run_agent_directly(
    executor_cls: type,
    settings: Settings,
    skill: str,
) -> str:
    """Run an agent executor directly without the A2A server.

    Used by CLI commands to invoke agent logic in-process rather than
    through the JSON-RPC transport.

    Args:
        executor_cls: The ``AgentExecutor`` subclass to instantiate.
        settings: Application settings.
        skill: The skill/command string to pass to the executor.

    Returns:
        Concatenated text output from the executor.
    """
    from bcn.common.db import close_pool
    from bcn.common.db import get_pool

    await get_pool(settings)
    executor = executor_cls(settings)

    from a2a.server.agent_execution import RequestContext
    from a2a.types import Message
    from a2a.types import MessageSendParams
    from a2a.types import TextPart

    class ResultCapture:
        """Lightweight event-queue stand-in that captures agent text output."""

        def __init__(self) -> None:
            self.messages: list[str] = []
            self._events: list[Any] = []

        def enqueue_event(self, event: Any) -> None:
            """Capture text parts from an agent event."""
            self._events.append(event)
            try:
                parts = event.parts if hasattr(event, "parts") else []
                for part in parts:
                    if hasattr(part, "text"):
                        self.messages.append(part.text)
            except Exception:
                pass

    capture = ResultCapture()

    message = Message(
        role="user",
        parts=[TextPart(text=skill)],
        message_id=uuid4().hex,
    )
    params = MessageSendParams(message=message)
    context = RequestContext(request=params)

    try:
        await executor.execute(context=context, event_queue=capture)
        return "\n".join(capture.messages) if capture.messages else "Done"
    finally:
        try:
            close_fn = getattr(executor, "close", None)
            if callable(close_fn):
                maybe = close_fn()
                if hasattr(maybe, "__await__"):
                    await maybe
        except Exception:
            logger.exception("Failed to close %s executor", executor_cls.__name__)
        finally:
            await close_pool()


# ---------------------------------------------------------------------------
# CLI Commands
# ---------------------------------------------------------------------------


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
def cli(verbose: bool) -> None:
    """Broken Cloud News - Cloud Security Briefing Agent."""
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)


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
        from bcn.agents.collector.agent import CollectorExecutor
        from bcn.common.db import close_pool
        from bcn.common.db import get_pool

        await get_pool(settings)
        executor = CollectorExecutor(settings)
        try:
            if source == "ghsa":
                count = await executor._collect_ghsa()
                click.echo(f"GHSA: collected {count} items")
            elif source == "rss":
                count = await executor._collect_rss()
                click.echo(f"RSS: collected {count} items")
            elif source == "twitter":
                count = await executor._collect_twitter()
                click.echo(f"Twitter: collected {count} items")
            elif source == "reddit":
                count = await executor._collect_reddit()
                click.echo(f"Reddit: collected {count} items")
            else:
                counts = await executor._collect_all()
                click.echo(
                    f"All: GHSA={counts[0]}, RSS={counts[1]}, "
                    f"Twitter={counts[2]}, Reddit={counts[3]}"
                )
        finally:
            try:
                await executor.close()
            except Exception:
                logger.exception("Failed to close collector executor")
            finally:
                await close_pool()

    asyncio.run(_run())


@cli.command()
def analyze() -> None:
    """Score and summarize unprocessed news items via the LLM."""
    settings = Settings()

    async def _run():
        from bcn.agents.analyst.agent import AnalystExecutor

        result = await _run_agent_directly(
            executor_cls=AnalystExecutor,
            settings=settings,
            skill="analyze_new_items",
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
        from bcn.agents.writer.agent import WriterExecutor

        result = await _run_agent_directly(
            executor_cls=WriterExecutor,
            settings=settings,
            skill=f"generate_briefing::{mode}",
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
        from bcn.agents.critic.agent import CriticExecutor

        skill: str
        if text_input:
            skill = f"critique_markdown::{text_input}"
        elif file_path:
            body = Path(file_path).read_text(encoding="utf-8")
            skill = f"critique_markdown::{body}"
        else:
            # Default to latest briefing to make this command useful out-of-the-box.
            skill = (
                "critique_latest"
                if latest or (not file_path and not text_input)
                else ""
            )

        result = await _run_agent_directly(
            executor_cls=CriticExecutor,
            settings=settings,
            skill=skill,
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
        from bcn.agents.verifier.agent import VerifierExecutor

        skill: str
        if text_input:
            skill = f"verify_markdown::{text_input}"
        elif file_path:
            body = Path(file_path).read_text(encoding="utf-8")
            skill = f"verify_markdown::{body}"
        else:
            skill = (
                "verify_latest" if latest or (not file_path and not text_input) else ""
            )

        result = await _run_agent_directly(
            executor_cls=VerifierExecutor,
            settings=settings,
            skill=skill,
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
        from bcn.common.db import close_pool
        from bcn.common.db import count_simulation_runs
        from bcn.common.db import ensure_simulation_tables
        from bcn.common.db import get_latest_simulation_report
        from bcn.common.db import get_pool
        from bcn.common.db import insert_simulation_report
        from bcn.simulation import compare_simulation_reports
        from bcn.simulation import simulate_historical_briefings

        await get_pool(settings)
        out_file = Path(output_path)
        baseline_report: dict[str, Any] | None = None

        if store_db:
            await ensure_simulation_tables()
            existing_runs = await count_simulation_runs()
            if existing_runs == 0 and out_file.exists():
                try:
                    previous_payload = json.loads(out_file.read_text(encoding="utf-8"))
                    if isinstance(previous_payload, dict) and isinstance(
                        previous_payload.get("results"), list
                    ):
                        imported_id = await insert_simulation_report(
                            previous_payload,
                            report_path=str(out_file),
                            source="imported_file",
                            notes="Imported from existing simulation output file.",
                        )
                        click.echo(
                            f"Imported baseline simulation from {output_path} (run_id={imported_id})"
                        )
                except Exception as exc:
                    click.echo(
                        f"Skipped baseline import from {output_path}: {exc}", err=True
                    )
            baseline_report = await get_latest_simulation_report()

        report = await simulate_historical_briefings(
            settings=settings,
            limit=max(0, int(limit)),
            since_days=max(0, int(since_days)),
            include_text=include_text,
            apply_critic_rewrites=with_critic_rewrites,
            reanalyze_items=reanalyze_items,
        )

        if store_db:
            run_id = await insert_simulation_report(
                report,
                report_path=str(out_file),
                source="cli",
            )
            report["db_run_id"] = str(run_id)
            if baseline_report:
                comparison = compare_simulation_reports(report, baseline_report)
                comparison["baseline_db_run_id"] = baseline_report.get("db_run_id")
                report["comparison_to_previous_run"] = comparison

        out_file.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
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
        from bcn.agents.distributor.agent import DistributorExecutor

        skill = f"distribute_briefing::{mode}"
        if briefing_id:
            try:
                parsed_briefing_id = UUID(briefing_id)
            except ValueError as exc:
                raise click.ClickException(
                    f"Invalid briefing UUID: {briefing_id}"
                ) from exc
            skill = f"distribute_briefing::{parsed_briefing_id}::{mode}"

        result = await _run_agent_directly(
            executor_cls=DistributorExecutor,
            settings=settings,
            skill=skill,
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


@cli.command("backfill-reddit-links")
@click.option(
    "--limit",
    type=int,
    default=0,
    show_default=True,
    help="Max Reddit rows to scan (0 = all).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Only report candidate URL rewrites; do not update DB.",
)
def backfill_reddit_links(limit: int, dry_run: bool) -> None:
    """Backfill existing Reddit rows with outbound destination URLs."""
    if limit < 0:
        raise click.ClickException("--limit must be >= 0")

    settings = Settings()

    async def _run() -> None:
        from bcn.common.db import close_pool
        from bcn.common.db import get_pool
        from bcn.common.scraper import Scraper

        await get_pool(settings)
        pool = await get_pool()
        scraper = Scraper(
            content_limit=settings.scrape_content_limit,
            min_content_length=settings.scrape_min_content_length,
        )

        scanned = 0
        candidates = 0
        updated = 0
        missing_post_id = 0
        fetch_failed = 0
        no_outbound = 0
        unchanged = 0
        samples: list[str] = []

        query = """
            SELECT id, source_id, url, raw_data
            FROM news_items
            WHERE source_type = 'reddit'
            ORDER BY published_at DESC
        """
        params: list[Any] = []
        if limit > 0:
            query += " LIMIT $1"
            params.append(limit)

        try:
            rows = await pool.fetch(query, *params)
            scanned = len(rows)
            for row in rows:
                current_url = str(row["url"] or "").strip()
                if not current_url or not _is_internal_reddit_url(current_url):
                    continue
                candidates += 1

                source_id = str(row["source_id"] or "").strip()
                raw_data = row["raw_data"]
                raw: dict[str, Any]
                if isinstance(raw_data, dict):
                    raw = raw_data
                elif isinstance(raw_data, str):
                    try:
                        parsed = json.loads(raw_data)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        parsed = {}
                    raw = parsed if isinstance(parsed, dict) else {}
                else:
                    raw = {}

                post_id = _extract_reddit_post_id(source_id, current_url)
                if not post_id:
                    post_id = _extract_reddit_post_id(
                        source_id,
                        str(raw.get("permalink") or raw.get("link") or ""),
                    )
                if not post_id:
                    missing_post_id += 1
                    continue

                comments_url = f"https://www.reddit.com/comments/{post_id}/.json"
                try:
                    payload_text = await scraper.fetch_text_or_raise(
                        comments_url,
                        headers={
                            "User-Agent": "BrokenCloudNews/1.0 (cloud-security digest bot)"
                        },
                        timeout_ms=20000,
                    )
                    payload = json.loads(payload_text)
                    post_data = _extract_reddit_listing_post_data(payload)
                except Exception:
                    fetch_failed += 1
                    continue

                outbound_url = str(
                    post_data.get("url_overridden_by_dest") or post_data.get("url") or ""
                ).strip()
                if (
                    not outbound_url.startswith(("http://", "https://"))
                    or _is_internal_reddit_url(outbound_url)
                ):
                    no_outbound += 1
                    continue

                if outbound_url.rstrip("/") == current_url.rstrip("/"):
                    unchanged += 1
                    continue

                permalink = _normalize_reddit_permalink(
                    str(
                        post_data.get("permalink")
                        or raw.get("permalink")
                        or raw.get("link")
                        or current_url
                    )
                )
                patch = {
                    "permalink": permalink,
                    "link": permalink,
                    "references": [{"url": outbound_url}],
                }
                if not dry_run:
                    await pool.execute(
                        """
                        UPDATE news_items
                        SET url = $1,
                            raw_data = COALESCE(raw_data, '{}'::jsonb) || $2::jsonb,
                            updated_at = NOW()
                        WHERE id = $3
                        """,
                        outbound_url,
                        json.dumps(patch, ensure_ascii=False),
                        row["id"],
                    )
                updated += 1
                if len(samples) < 5:
                    samples.append(f"{post_id}: {current_url} -> {outbound_url}")
        finally:
            try:
                await scraper.close()
            finally:
                await close_pool()

        click.echo(
            "Reddit backfill complete: "
            f"scanned={scanned}, candidates={candidates}, "
            f"updated={updated}, missing_post_id={missing_post_id}, "
            f"fetch_failed={fetch_failed}, no_outbound={no_outbound}, "
            f"unchanged={unchanged}, dry_run={str(dry_run).lower()}"
        )
        if samples:
            click.echo("Sample rewrites:")
            for sample in samples:
                click.echo(f"- {sample}")

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
def export_training(
    output_dir: str,
    limit: int,
    since_days: int,
    include_blocked: bool,
) -> None:
    """Export SFT + preference JSONL datasets from stored traces."""
    settings = Settings()

    async def _run() -> None:
        from datetime import datetime
        from uuid import UUID

        from bcn.common.db import close_pool
        from bcn.common.db import get_distribution_outcomes
        from bcn.common.db import get_generation_preference_pairs_for_runs
        from bcn.common.db import get_generation_rounds_for_runs
        from bcn.common.db import get_generation_runs_for_export
        from bcn.common.db import get_human_reviews
        from bcn.common.db import get_pool

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
        if not runs:
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

        with sft_path.open("w", encoding="utf-8") as handle:
            for row in sft_rows:
                handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        with pref_path.open("w", encoding="utf-8") as handle:
            for row in pref_rows:
                handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        with trace_path.open("w", encoding="utf-8") as handle:
            for row in trace_rows:
                handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

        manifest = {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "run_count": len(runs),
            "sft_rows": len(sft_rows),
            "preference_rows": len(pref_rows),
            "trace_rows": len(trace_rows),
            "filters": {
                "limit": int(limit),
                "since_days": int(since_days),
                "include_blocked": bool(include_blocked),
            },
            "files": {
                "sft_jsonl": str(sft_path),
                "preference_jsonl": str(pref_path),
                "trace_jsonl": str(trace_path),
            },
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        click.echo(
            f"Export complete: runs={len(runs)} sft_rows={len(sft_rows)} "
            f"preference_rows={len(pref_rows)}"
        )
        click.echo(f"  SFT: {sft_path}")
        click.echo(f"  Preference: {pref_path}")
        click.echo(f"  Traces: {trace_path}")
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
        from bcn.agents.distributor.agent import DistributorExecutor
        from bcn.agents.writer.agent import WriterExecutor

        async def _run_writer(skill: str) -> str:
            return await _run_agent_directly(
                executor_cls=WriterExecutor,
                settings=settings,
                skill=skill,
            )

        async def _run_distributor(skill: str) -> str:
            return await _run_agent_directly(
                executor_cls=DistributorExecutor,
                settings=settings,
                skill=skill,
            )

        writer_result, distribute_result = await run_writer_distributor_handoff(
            mode=mode,
            run_writer=_run_writer,
            run_distributor=_run_distributor,
        )
        click.echo(writer_result)
        if not distribute_result:
            click.echo("Writer did not return a briefing id; skipping distribution.")
            return

        click.echo(distribute_result)

    asyncio.run(_run())


@cli.command()
def run() -> None:
    """Start daemon mode with all A2A agents and the scheduler."""
    settings = Settings()

    async def _daemon() -> None:
        from apscheduler import AsyncScheduler
        from apscheduler.triggers.interval import IntervalTrigger

        from bcn.agents.analyst.agent import AnalystExecutor
        from bcn.agents.analyst.agent import SKILLS as ANAL_SKILLS
        from bcn.agents.base import build_agent_card
        from bcn.agents.base import serve_agent
        from bcn.agents.collector.agent import CollectorExecutor
        from bcn.agents.collector.agent import SKILLS as COLL_SKILLS
        from bcn.agents.critic.agent import CriticExecutor
        from bcn.agents.critic.agent import SKILLS as CRIT_SKILLS
        from bcn.agents.distributor.agent import DistributorExecutor
        from bcn.agents.distributor.agent import SKILLS as DIST_SKILLS
        from bcn.agents.verifier.agent import SKILLS as VERI_SKILLS
        from bcn.agents.verifier.agent import VerifierExecutor
        from bcn.agents.writer.agent import SKILLS as WRIT_SKILLS
        from bcn.agents.writer.agent import WriterExecutor
        from bcn.common.db import close_pool
        from bcn.common.db import finalize_stale_pending_generation_runs
        from bcn.common.db import get_pool

        async def _send_with_runtime_timeout(port: int, skill: str) -> str:
            return await _send_to_agent(
                port,
                skill,
                timeout_seconds=settings.a2a_request_timeout_seconds,
            )

        configure_scheduler_runtime(settings, _send_with_runtime_timeout)

        await get_pool(settings)
        try:
            finalized = await finalize_stale_pending_generation_runs(
                max_age_minutes=max(
                    1, int(getattr(settings, "generation_run_stale_pending_minutes", 180))
                ),
                decision="BLOCKED",
                decision_reason="daemon_auto_finalize_stale_pending_run",
            )
            if finalized:
                logger.warning(
                    "Auto-finalized %d stale PENDING generation runs during daemon startup",
                    finalized,
                )
        except Exception:
            logger.exception("Failed to auto-finalize stale PENDING generation runs")

        click.echo("Starting Broken Cloud News agents...")

        # Build agent cards
        collector_card = build_agent_card(
            "BCN Collector",
            "Collects cloud security news from GHSA, Twitter, RSS, Reddit",
            f"http://localhost:{settings.collector_port}/",
            COLL_SKILLS,
        )
        analyst_card = build_agent_card(
            "BCN Analyst",
            "Analyzes news items for relevance scoring",
            f"http://localhost:{settings.analyst_port}/",
            ANAL_SKILLS,
        )
        writer_card = build_agent_card(
            "BCN Writer",
            "Generates security briefings with cover images",
            f"http://localhost:{settings.writer_port}/",
            WRIT_SKILLS,
        )
        distributor_card = build_agent_card(
            "BCN Distributor",
            "Distributes briefings to Telegram/Discord (daily, ad-hoc) or Email (monthly)",
            f"http://localhost:{settings.distributor_port}/",
            DIST_SKILLS,
        )
        critic_card = build_agent_card(
            "BCN Critic",
            "Critiques briefing quality and provides recommendations",
            f"http://localhost:{settings.critic_port}/",
            CRIT_SKILLS,
        )
        verifier_card = build_agent_card(
            "BCN Verifier",
            "Verifies briefing facts, links, and top-story quality",
            f"http://localhost:{settings.verifier_port}/",
            VERI_SKILLS,
        )

        # Create executors
        collector_exec = CollectorExecutor(settings)
        analyst_exec = AnalystExecutor(settings)
        writer_exec = WriterExecutor(settings)
        distributor_exec = DistributorExecutor(settings)
        critic_exec = CriticExecutor(settings)
        verifier_exec = VerifierExecutor(settings)
        executors = [
            collector_exec,
            analyst_exec,
            writer_exec,
            distributor_exec,
            critic_exec,
            verifier_exec,
        ]

        # Launch agent servers
        tasks: list[asyncio.Task[Any]] = []

        click.echo(f"  Collector on :{settings.collector_port}")
        click.echo(f"  Analyst  on :{settings.analyst_port}")
        click.echo(f"  Writer   on :{settings.writer_port}")
        click.echo(f"  Distributor on :{settings.distributor_port}")
        click.echo(f"  Critic on :{settings.critic_port}")
        click.echo(f"  Verifier on :{settings.verifier_port}")

        try:
            tasks = [
                asyncio.create_task(
                    serve_agent(collector_card, collector_exec, settings.collector_port)
                ),
                asyncio.create_task(
                    serve_agent(analyst_card, analyst_exec, settings.analyst_port)
                ),
                asyncio.create_task(
                    serve_agent(writer_card, writer_exec, settings.writer_port)
                ),
                asyncio.create_task(
                    serve_agent(
                        distributor_card, distributor_exec, settings.distributor_port
                    )
                ),
                asyncio.create_task(
                    serve_agent(critic_card, critic_exec, settings.critic_port)
                ),
                asyncio.create_task(
                    serve_agent(verifier_card, verifier_exec, settings.verifier_port)
                ),
            ]

            # Set up scheduler (job functions are module-level for APScheduler 4.x)
            async with AsyncScheduler() as scheduler:
                # Collection schedules
                await scheduler.add_schedule(
                    job_collect_ghsa,
                    IntervalTrigger(hours=settings.ghsa_interval_hours),
                    id="ghsa_collector",
                )
                await scheduler.add_schedule(
                    job_collect_rss,
                    IntervalTrigger(hours=settings.rss_interval_hours),
                    id="rss_collector",
                )
                await scheduler.add_schedule(
                    job_collect_reddit,
                    IntervalTrigger(hours=settings.reddit_interval_hours),
                    id="reddit_collector",
                )
                await scheduler.add_schedule(
                    job_collect_twitter,
                    IntervalTrigger(hours=settings.twitter_interval_hours),
                    id="twitter_collector",
                )

                # Analyst schedule
                await scheduler.add_schedule(
                    job_analyze_items,
                    IntervalTrigger(minutes=settings.analyst_interval_minutes),
                    id="analyst",
                )

                # Regular briefing cycle: write + distribute
                await scheduler.add_schedule(
                    _job_daily_digest,
                    build_regular_briefing_trigger(settings),
                    id=REGULAR_DAILY_BRIEFING_MODE,
                )
                if settings.monthly_newsletter_enabled:
                    await scheduler.add_schedule(
                        _job_monthly_newsletter,
                        build_regular_monthly_newsletter_trigger(settings),
                        id=REGULAR_MONTHLY_NEWSLETTER_MODE,
                    )

                await scheduler.start_in_background()
                click.echo("Scheduler started. Press Ctrl+C to stop.")
                await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            for executor in executors:
                close_fn = getattr(executor, "close", None)
                if callable(close_fn):
                    try:
                        maybe = close_fn()
                        if hasattr(maybe, "__await__"):
                            await maybe
                    except Exception:
                        logger.exception(
                            "Failed to close %s executor", executor.__class__.__name__
                        )
            try:
                await close_pool()
            except Exception:
                logger.exception("Failed to close DB pool during daemon shutdown")

    try:
        asyncio.run(_daemon())
    except KeyboardInterrupt:
        click.echo("\nShutting down...")
