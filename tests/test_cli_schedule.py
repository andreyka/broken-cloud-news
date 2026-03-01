from __future__ import annotations

from datetime import datetime
from datetime import timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from click.testing import CliRunner

import bcn.cli as cli_module
from bcn.common.config import Settings
from bcn.workflows.automation import build_regular_briefing_trigger
from bcn.workflows.automation import build_regular_monthly_newsletter_trigger
from bcn.workflows.automation import configure_scheduler_runtime
from bcn.workflows.automation import extract_briefing_id
from bcn.workflows.automation import job_publish_regular_monthly_newsletter
from bcn.workflows.automation import job_publish_regular_briefing
from bcn.workflows.modes import REGULAR_DAILY_BRIEFING_MODE
from bcn.workflows.modes import REGULAR_MONTHLY_NEWSLETTER_MODE


def test_build_regular_briefing_trigger_from_multi_hours():
    settings = Settings(
        distribute_hours=[19, 9, 13, 9],
        distribute_minute=0,
        distribute_timezone="UTC",
    )
    trigger = build_regular_briefing_trigger(settings)
    assert trigger.hour == "9,13,19"
    assert trigger.minute == 0
    assert str(trigger.timezone) == "UTC"


def test_build_regular_briefing_trigger_legacy_fallback_hour():
    settings = Settings(
        distribute_hour=9,
        distribute_hours=[],
        distribute_minute=15,
        distribute_timezone="America/Los_Angeles",
    )
    trigger = build_regular_briefing_trigger(settings)
    assert trigger.hour == "9"
    assert trigger.minute == 15
    assert str(trigger.timezone) == "America/Los_Angeles"


def test_extract_briefing_id_from_writer_message():
    briefing_id = uuid4()
    result = extract_briefing_id(f"Briefing created: id={briefing_id} items=3")
    assert result == briefing_id


def test_build_regular_monthly_newsletter_trigger():
    settings = Settings(
        monthly_newsletter_day=3,
        monthly_newsletter_hour=17,
        monthly_newsletter_minute=5,
        monthly_newsletter_timezone="America/Los_Angeles",
    )
    trigger = build_regular_monthly_newsletter_trigger(settings)
    assert trigger.day == 3
    assert trigger.hour == 17
    assert trigger.minute == 5
    assert str(trigger.timezone) == "America/Los_Angeles"


@pytest.mark.asyncio
async def test_job_publish_regular_briefing_distributes_target_briefing():
    settings = Settings(writer_port=9003, distributor_port=9004)
    briefing_id = uuid4()
    send_mock = AsyncMock(
        side_effect=[
            f"Briefing created: id={briefing_id} items=3",
            "Distributed to: {'telegram': 'ok'}",
        ]
    )
    configure_scheduler_runtime(settings, send_mock)

    await job_publish_regular_briefing()

    assert send_mock.await_count == 2
    assert (
        send_mock.await_args_list[0].args
        == (9003, f"generate_briefing::{REGULAR_DAILY_BRIEFING_MODE}")
    )
    assert (
        send_mock.await_args_list[1].args
        == (
            9004,
            f"distribute_briefing::{briefing_id}::{REGULAR_DAILY_BRIEFING_MODE}",
        )
    )


@pytest.mark.asyncio
async def test_job_publish_regular_briefing_skips_distribution_when_writer_returns_no_id():
    settings = Settings(writer_port=9003, distributor_port=9004)
    send_mock = AsyncMock(return_value="Quiet day — no items scored >= 7")
    configure_scheduler_runtime(settings, send_mock)

    await job_publish_regular_briefing()

    assert send_mock.await_count == 1
    assert (
        send_mock.await_args_list[0].args
        == (9003, f"generate_briefing::{REGULAR_DAILY_BRIEFING_MODE}")
    )


@pytest.mark.asyncio
async def test_job_publish_regular_monthly_newsletter_uses_monthly_mode():
    settings = Settings(writer_port=9003, distributor_port=9004)
    briefing_id = uuid4()
    send_mock = AsyncMock(
        side_effect=[
            f"Briefing created: id={briefing_id} items=11",
            "Distributed to: {'email': 'ok'}",
        ]
    )
    configure_scheduler_runtime(settings, send_mock)

    await job_publish_regular_monthly_newsletter()

    assert send_mock.await_count == 2
    assert (
        send_mock.await_args_list[0].args
        == (9003, f"generate_briefing::{REGULAR_MONTHLY_NEWSLETTER_MODE}")
    )
    assert (
        send_mock.await_args_list[1].args
        == (
            9004,
            f"distribute_briefing::{briefing_id}::{REGULAR_MONTHLY_NEWSLETTER_MODE}",
        )
    )


def test_distribute_command_delegates_to_distributor_executor(monkeypatch):
    runner = CliRunner()
    target_id = uuid4()
    run_mock = AsyncMock(return_value="Distributed to: {'telegram': 'ok'}")
    monkeypatch.setattr(cli_module, "_run_agent_directly", run_mock)

    result = runner.invoke(
        cli_module.cli,
        ["distribute", "--briefing-id", str(target_id)],
    )

    assert result.exit_code == 0
    assert "Distributed to:" in result.output
    assert run_mock.await_count == 1
    kwargs = run_mock.await_args.kwargs
    assert (
        kwargs["skill"]
        == f"distribute_briefing::{target_id}::{REGULAR_DAILY_BRIEFING_MODE}"
    )

    from bcn.agents.distributor.agent import DistributorExecutor

    assert kwargs["executor_cls"] is DistributorExecutor


def test_newsletter_subscribers_list_command(monkeypatch):
    runner = CliRunner()
    now = datetime.now(timezone.utc)
    monkeypatch.setattr("bcn.common.db.get_pool", AsyncMock())
    monkeypatch.setattr("bcn.common.db.close_pool", AsyncMock())
    monkeypatch.setattr(
        "bcn.common.db.get_newsletter_subscribers",
        AsyncMock(
            return_value=[
                {
                    "email": "alice@example.com",
                    "is_active": True,
                    "updated_at": now,
                }
            ]
        ),
    )

    result = runner.invoke(cli_module.cli, ["newsletter-subscribers", "list"])

    assert result.exit_code == 0
    assert "alice@example.com | status=active" in result.output


def test_newsletter_subscribers_add_command(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr("bcn.common.db.get_pool", AsyncMock())
    monkeypatch.setattr("bcn.common.db.close_pool", AsyncMock())
    add_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("bcn.common.db.add_newsletter_subscriber", add_mock)

    result = runner.invoke(
        cli_module.cli,
        ["newsletter-subscribers", "add", "alice@example.com"],
    )

    assert result.exit_code == 0
    assert "Added newsletter subscriber: alice@example.com" in result.output
    assert add_mock.await_count == 1


def test_newsletter_subscribers_remove_command(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr("bcn.common.db.get_pool", AsyncMock())
    monkeypatch.setattr("bcn.common.db.close_pool", AsyncMock())
    remove_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("bcn.common.db.remove_newsletter_subscriber", remove_mock)

    result = runner.invoke(
        cli_module.cli,
        ["newsletter-subscribers", "remove", "alice@example.com"],
    )

    assert result.exit_code == 0
    assert "Removed newsletter subscriber: alice@example.com" in result.output
    assert remove_mock.await_count == 1
