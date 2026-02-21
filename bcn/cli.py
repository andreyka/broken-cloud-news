"""CLI entry-point and daemon mode for Broken Cloud News."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

import click
import httpx

from bcn.config import Settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("bcn")


# Module-level settings reference (set by the ``run`` command for scheduler jobs).
_settings: Settings | None = None

# ---------------------------------------------------------------------------
# A2A client helpers
# ---------------------------------------------------------------------------


async def _send_to_agent(port: int, skill: str) -> str:
    """Send a JSON-RPC message to a local A2A agent and return its reply.

    Args:
        port: TCP port the target agent is listening on.
        skill: The skill/command string to send.

    Returns:
        The text content extracted from the agent's response.
    """
    from a2a.client import A2AClient
    from a2a.types import MessageSendParams, SendMessageRequest, Message, TextPart

    timeout = (_settings.a2a_request_timeout_seconds if _settings else 180)
    async with httpx.AsyncClient(timeout=timeout) as http_client:
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


# ---------------------------------------------------------------------------
# Scheduler job functions (must be top-level for APScheduler 4.x serialization)
# ---------------------------------------------------------------------------


async def _job_collect_ghsa() -> None:
    """Scheduled job: trigger GHSA collection."""
    await _send_to_agent(_settings.collector_port, "collect_ghsa")


async def _job_collect_rss() -> None:
    """Scheduled job: trigger RSS collection."""
    await _send_to_agent(_settings.collector_port, "collect_rss")


async def _job_collect_twitter() -> None:
    """Scheduled job: trigger Twitter/X collection."""
    await _send_to_agent(_settings.collector_port, "collect_twitter")


async def _job_collect_reddit() -> None:
    """Scheduled job: trigger Reddit collection."""
    await _send_to_agent(_settings.collector_port, "collect_reddit")


async def _job_analyze() -> None:
    """Scheduled job: trigger item analysis."""
    await _send_to_agent(_settings.analyst_port, "analyze_new_items")


async def _job_daily_digest() -> None:
    """Scheduled job: generate and distribute a daily briefing."""
    await _send_to_agent(_settings.writer_port, "generate_briefing")
    await _send_to_agent(_settings.distributor_port, "distribute_briefing")


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
    from bcn.db import get_pool, close_pool

    await get_pool(settings)
    executor = executor_cls(settings)

    from a2a.server.agent_execution import RequestContext
    from a2a.types import MessageSendParams, Message, TextPart

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

    await executor.execute(context=context, event_queue=capture)

    await close_pool()
    return "\n".join(capture.messages) if capture.messages else "Done"


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
    "--source", "-s",
    type=click.Choice(["all", "ghsa", "rss", "twitter", "reddit"]),
    default="all",
)
def collect(source: str) -> None:
    """Run collector for all sources or a specific one."""
    settings = Settings()

    async def _run():
        from bcn.agents.collector import CollectorExecutor
        from bcn.db import get_pool, close_pool

        await get_pool(settings)
        executor = CollectorExecutor(settings)

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

        await close_pool()

    asyncio.run(_run())


@cli.command()
def analyze() -> None:
    """Score and summarize unprocessed news items via the LLM."""
    settings = Settings()

    async def _run():
        from bcn.agents.analyst import AnalystExecutor
        from bcn.db import get_pool, close_pool, get_new_items, update_item_analyzed
        from bcn.llm import LLMClient
        from bcn.scraper import Scraper

        await get_pool(settings)
        llm = LLMClient(settings.llm_base_url, settings.llm_model, settings.llm_timeout)
        scraper = Scraper(settings.scrape_content_limit, settings.scrape_min_content_length)

        items = await get_new_items()
        if not items:
            click.echo("No new items to analyze")
            return

        analyzed = 0
        for item in items:
            title = item["title"] or ""
            content = item["full_content"] or ""

            if not content and item["source_type"] == "rss" and item["url"]:
                content = await scraper.scrape(item["url"])

            if not content:
                content = title

            try:
                result = await llm.analyze_item(title, content)
                await update_item_analyzed(
                    item_id=item["id"],
                    summary=result.summary,
                    relevance_score=result.relevance_score,
                    ai_tags=result.tags,
                    full_content=content if content != title else item["full_content"],
                    image_prompt=result.image_prompt,
                )
                analyzed += 1
                click.echo(f"  [{result.relevance_score}/10] {item['source_type']}: {title[:60]}")
            except Exception as exc:
                click.echo(f"  [ERROR] {item['id']}: {type(exc).__name__}: {exc or repr(exc)}", err=True)

        click.echo(f"\nAnalyzed {analyzed}/{len(items)} items")
        await llm.close()
        await scraper.close()
        await close_pool()

    asyncio.run(_run())


@cli.command()
def write() -> None:
    """Generate a briefing with cover image from top-scored items."""
    settings = Settings()

    async def _run():
        from bcn.agents.writer import WriterExecutor

        result = await _run_agent_directly(
            executor_cls=WriterExecutor,
            settings=settings,
            skill="generate_briefing",
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
        from bcn.agents.critic import CriticExecutor

        skill: str
        if text_input:
            skill = f"critique_markdown::{text_input}"
        elif file_path:
            body = Path(file_path).read_text(encoding="utf-8")
            skill = f"critique_markdown::{body}"
        else:
            # Default to latest briefing to make this command useful out-of-the-box.
            skill = "critique_latest" if latest or (not file_path and not text_input) else ""

        result = await _run_agent_directly(
            executor_cls=CriticExecutor,
            settings=settings,
            skill=skill,
        )
        click.echo(result)

    asyncio.run(_run())


@cli.command()
def distribute() -> None:
    """Send the latest briefing to configured distribution channels."""
    settings = Settings()

    async def _run():
        from bcn.db import get_pool, close_pool, get_latest_briefing, mark_briefing_distributed, mark_items_published
        from bcn.distributors.telegram import TelegramDistributor
        from bcn.distributors.email import EmailDistributor
        from bcn.distributors.slack import SlackDistributor
        from datetime import datetime, timezone

        await get_pool(settings)
        briefing = await get_latest_briefing()
        if not briefing:
            click.echo("No new briefing to distribute")
            await close_pool()
            return

        channels: list[tuple[str, TelegramDistributor | EmailDistributor | SlackDistributor]] = []
        if settings.telegram_bot_token and settings.telegram_chat_id:
            channels.append((
                "telegram",
                TelegramDistributor(
                    settings.telegram_bot_token,
                    settings.telegram_chat_id,
                    overflow_mode=settings.telegram_overflow_mode,
                ),
            ))
        if settings.smtp_host and settings.email_recipients:
            channels.append(("email", EmailDistributor(
                settings.smtp_host, settings.smtp_port, settings.smtp_user,
                settings.smtp_password, settings.email_from, settings.email_recipients,
            )))
        if settings.slack_webhook_url:
            channels.append(("slack", SlackDistributor(settings.slack_webhook_url)))

        if not channels:
            click.echo("No distribution channels configured")
            await close_pool()
            return

        results: dict[str, str] = {}
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        for name, channel in channels:
            try:
                if name == "telegram":
                    ok = await channel.send(briefing["content_markdown"], briefing["cover_image_url"])
                elif name == "email":
                    ok = await channel.send(
                        f"Broken Cloud News - {today}",
                        briefing["content_html"] or briefing["content_markdown"],
                    )
                elif name == "slack":
                    ok = await channel.send(briefing["content_markdown"], briefing["cover_image_url"])
                else:
                    ok = False
                results[name] = "ok" if ok else "failed"
                click.echo(f"  {name}: {'ok' if ok else 'failed'}")
            except Exception as exc:
                results[name] = f"error: {exc}"
                click.echo(f"  {name}: error - {exc}", err=True)

        await mark_briefing_distributed(briefing["id"], results)
        item_ids = list(briefing["item_ids"]) if briefing["item_ids"] else []
        await mark_items_published(item_ids)
        click.echo("Distribution complete")
        await close_pool()

    asyncio.run(_run())


@cli.command()
def pipeline() -> None:
    """Run the full pipeline: collect, analyze, write, distribute."""
    settings = Settings()
    ctx = click.get_current_context()

    click.echo("=== COLLECT ===")
    ctx.invoke(collect, source="all")
    click.echo("\n=== ANALYZE ===")
    ctx.invoke(analyze)
    click.echo("\n=== WRITE ===")
    ctx.invoke(write)
    click.echo("\n=== DISTRIBUTE ===")
    ctx.invoke(distribute)
    click.echo("\nPipeline complete.")


@cli.command()
def run() -> None:
    """Start daemon mode with all A2A agents and the scheduler."""
    global _settings
    settings = Settings()
    _settings = settings

    async def _daemon():
        from bcn.agents.base import build_agent_card, serve_agent
        from bcn.agents.collector import CollectorExecutor, SKILLS as COLL_SKILLS
        from bcn.agents.analyst import AnalystExecutor, SKILLS as ANAL_SKILLS
        from bcn.agents.writer import WriterExecutor, SKILLS as WRIT_SKILLS
        from bcn.agents.distributor import DistributorExecutor, SKILLS as DIST_SKILLS
        from bcn.agents.critic import CriticExecutor, SKILLS as CRIT_SKILLS
        from bcn.db import get_pool
        from apscheduler import AsyncScheduler
        from apscheduler.triggers.interval import IntervalTrigger
        from apscheduler.triggers.cron import CronTrigger

        await get_pool(settings)

        click.echo("Starting Broken Cloud News agents...")

        # Build agent cards
        collector_card = build_agent_card(
            "BCN Collector", "Collects cloud security news from GHSA, Twitter, RSS, Reddit",
            f"http://localhost:{settings.collector_port}/", COLL_SKILLS,
        )
        analyst_card = build_agent_card(
            "BCN Analyst", "Analyzes news items for relevance scoring",
            f"http://localhost:{settings.analyst_port}/", ANAL_SKILLS,
        )
        writer_card = build_agent_card(
            "BCN Writer", "Generates security briefings with cover images",
            f"http://localhost:{settings.writer_port}/", WRIT_SKILLS,
        )
        distributor_card = build_agent_card(
            "BCN Distributor", "Distributes briefings to Telegram, Email, Slack",
            f"http://localhost:{settings.distributor_port}/", DIST_SKILLS,
        )
        critic_card = build_agent_card(
            "BCN Critic", "Critiques briefing quality and provides recommendations",
            f"http://localhost:{settings.critic_port}/", CRIT_SKILLS,
        )

        # Create executors
        collector_exec = CollectorExecutor(settings)
        analyst_exec = AnalystExecutor(settings)
        writer_exec = WriterExecutor(settings)
        distributor_exec = DistributorExecutor(settings)
        critic_exec = CriticExecutor(settings)

        # Launch agent servers
        tasks = [
            asyncio.create_task(serve_agent(collector_card, collector_exec, settings.collector_port)),
            asyncio.create_task(serve_agent(analyst_card, analyst_exec, settings.analyst_port)),
            asyncio.create_task(serve_agent(writer_card, writer_exec, settings.writer_port)),
            asyncio.create_task(serve_agent(distributor_card, distributor_exec, settings.distributor_port)),
            asyncio.create_task(serve_agent(critic_card, critic_exec, settings.critic_port)),
        ]

        click.echo(f"  Collector on :{settings.collector_port}")
        click.echo(f"  Analyst  on :{settings.analyst_port}")
        click.echo(f"  Writer   on :{settings.writer_port}")
        click.echo(f"  Distributor on :{settings.distributor_port}")
        click.echo(f"  Critic on :{settings.critic_port}")

        # Set up scheduler (job functions are module-level for APScheduler 4.x)
        async with AsyncScheduler() as scheduler:
            # Collection schedules
            await scheduler.add_schedule(
                _job_collect_ghsa,
                IntervalTrigger(hours=settings.ghsa_interval_hours),
                id="ghsa_collector",
            )
            await scheduler.add_schedule(
                _job_collect_rss,
                IntervalTrigger(hours=settings.rss_interval_hours),
                id="rss_collector",
            )
            await scheduler.add_schedule(
                _job_collect_reddit,
                IntervalTrigger(hours=settings.reddit_interval_hours),
                id="reddit_collector",
            )
            await scheduler.add_schedule(
                _job_collect_twitter,
                IntervalTrigger(hours=settings.twitter_interval_hours),
                id="twitter_collector",
            )

            # Analyst schedule
            await scheduler.add_schedule(
                _job_analyze,
                IntervalTrigger(minutes=settings.analyst_interval_minutes),
                id="analyst",
            )

            # Daily digest: write + distribute
            await scheduler.add_schedule(
                _job_daily_digest,
                CronTrigger(hour=settings.distribute_hour, minute=settings.distribute_minute),
                id="daily_digest",
            )

            await scheduler.start_in_background()
            click.echo("Scheduler started. Press Ctrl+C to stop.")
            await asyncio.gather(*tasks)

    try:
        asyncio.run(_daemon())
    except KeyboardInterrupt:
        click.echo("\nShutting down...")
