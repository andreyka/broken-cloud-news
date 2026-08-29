"""Deployment smoke checks: one real call per LLM role plus infra probes."""

from __future__ import annotations

import asyncio
import sys
import time

import click

from bcn.cli_commands.shared import build_settings
from bcn.cli_commands.shared import run_async


def register_smoke_commands(cli: click.Group) -> None:
    """Attach the smoke command to the root CLI group."""

    @cli.command("smoke")
    @click.option("--timeout", type=int, default=90, show_default=True)
    def smoke(timeout: int) -> None:
        """Run one real request per LLM role and basic infra probes.

        Run after every deploy: a role that only fails under its real request
        shape (tools, reasoning params) will not be caught by anything else.
        """
        settings = build_settings()
        failures: list[str] = []

        async def _run() -> None:
            from bcn.common.llm import LLMClient
            from bcn.persistence.runtime import close_pool
            from bcn.persistence.runtime import get_pool
            from bcn.services.tools import fetch_page_content

            client = LLMClient.from_settings(settings)

            async def check(name: str, coro) -> None:
                started = time.monotonic()
                try:
                    await asyncio.wait_for(coro, timeout=timeout)
                    click.echo(f"PASS {name} ({time.monotonic() - started:.1f}s)")
                except Exception as exc:
                    failures.append(name)
                    click.echo(
                        f"FAIL {name}: {type(exc).__name__}: {str(exc)[:160]}"
                    )

            async def db_ping() -> None:
                pool = await get_pool(settings)
                await pool.fetchval("SELECT 1")

            await check("db", db_ping())
            for role, tools in (
                ("analyst", [fetch_page_content]),
                ("writer", [fetch_page_content]),
                ("critic", None),
                ("verifier", None),
            ):
                await check(
                    f"llm:{role}",
                    client.chat_for_role(
                        role=role,
                        system_prompt="Reply with exactly: ok",
                        user_content="say ok",
                        retries=1,
                        tools=tools,
                    ),
                )
            await close_pool()

        run_async(_run)
        if failures:
            click.echo(f"SMOKE FAILED: {', '.join(failures)}")
            sys.exit(1)
        click.echo("SMOKE OK: all roles and infra reachable")


__all__ = ["register_smoke_commands"]
