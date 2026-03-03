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
from bcn.workflows.modes.common import parse_writer_handoff_payload
from bcn.workflows.modes.common import render_writer_handoff_payload
from bcn.workflows.modes.common import run_writer_distributor_handoff


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


def test_parse_writer_handoff_payload_publish():
    briefing_id = uuid4()
    message = render_writer_handoff_payload(
        mode=REGULAR_DAILY_BRIEFING_MODE,
        decision="publish",
        briefing_id=briefing_id,
        item_count=3,
    )
    payload = parse_writer_handoff_payload(message)
    assert payload is not None
    assert payload.briefing_id == briefing_id
    assert payload.decision == "publish"
    assert payload.mode == REGULAR_DAILY_BRIEFING_MODE
    assert payload.item_count == 3


@pytest.mark.asyncio
async def test_run_writer_distributor_handoff_uses_shared_skill_format():
    briefing_id = uuid4()
    run_writer = AsyncMock(
        return_value=render_writer_handoff_payload(
            mode=REGULAR_DAILY_BRIEFING_MODE,
            decision="publish",
            briefing_id=briefing_id,
            item_count=3,
        )
    )
    run_distributor = AsyncMock(return_value="Distributed to: {'telegram': 'ok'}")

    writer_result, distributor_result = await run_writer_distributor_handoff(
        mode=REGULAR_DAILY_BRIEFING_MODE,
        run_writer=run_writer,
        run_distributor=run_distributor,
    )

    assert "writer_handoff::" in writer_result
    assert "Distributed to:" in str(distributor_result)
    run_writer.assert_awaited_once_with(
        f"generate_briefing::{REGULAR_DAILY_BRIEFING_MODE}"
    )
    run_distributor.assert_awaited_once_with(
        f"distribute_briefing::{briefing_id}::{REGULAR_DAILY_BRIEFING_MODE}"
    )


@pytest.mark.asyncio
async def test_run_writer_distributor_handoff_enforces_requested_mode():
    briefing_id = uuid4()
    run_writer = AsyncMock(
        return_value=render_writer_handoff_payload(
            mode=REGULAR_MONTHLY_NEWSLETTER_MODE,
            decision="publish",
            briefing_id=briefing_id,
            item_count=3,
        )
    )
    run_distributor = AsyncMock(return_value="Distributed to: {'telegram': 'ok'}")

    _writer_result, distributor_result = await run_writer_distributor_handoff(
        mode=REGULAR_DAILY_BRIEFING_MODE,
        run_writer=run_writer,
        run_distributor=run_distributor,
    )

    assert "Distributed to:" in str(distributor_result)
    run_distributor.assert_awaited_once_with(
        f"distribute_briefing::{briefing_id}::{REGULAR_DAILY_BRIEFING_MODE}"
    )


@pytest.mark.asyncio
async def test_run_writer_distributor_handoff_skips_distribution_on_skip_decision():
    run_writer = AsyncMock(
        return_value=render_writer_handoff_payload(
            mode=REGULAR_DAILY_BRIEFING_MODE,
            decision="skip",
            item_count=0,
        )
    )
    run_distributor = AsyncMock(return_value="unused")

    _writer_result, distributor_result = await run_writer_distributor_handoff(
        mode=REGULAR_DAILY_BRIEFING_MODE,
        run_writer=run_writer,
        run_distributor=run_distributor,
    )

    assert distributor_result is None
    run_writer.assert_awaited_once_with(
        f"generate_briefing::{REGULAR_DAILY_BRIEFING_MODE}"
    )
    run_distributor.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_writer_distributor_handoff_skips_unstructured_writer_output():
    briefing_id = uuid4()
    run_writer = AsyncMock(return_value=f"Briefing created: id={briefing_id} items=3")
    run_distributor = AsyncMock(return_value="unused")

    _writer_result, distributor_result = await run_writer_distributor_handoff(
        mode=REGULAR_DAILY_BRIEFING_MODE,
        run_writer=run_writer,
        run_distributor=run_distributor,
    )

    assert distributor_result is None
    run_writer.assert_awaited_once_with(
        f"generate_briefing::{REGULAR_DAILY_BRIEFING_MODE}"
    )
    run_distributor.assert_not_awaited()


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
            render_writer_handoff_payload(
                mode=REGULAR_DAILY_BRIEFING_MODE,
                decision="publish",
                briefing_id=briefing_id,
                item_count=3,
            ),
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
async def test_job_publish_regular_briefing_skips_distribution_when_writer_skips():
    settings = Settings(writer_port=9003, distributor_port=9004)
    send_mock = AsyncMock(
        return_value=render_writer_handoff_payload(
            mode=REGULAR_DAILY_BRIEFING_MODE,
            decision="skip",
            item_count=0,
        )
    )
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
            render_writer_handoff_payload(
                mode=REGULAR_MONTHLY_NEWSLETTER_MODE,
                decision="publish",
                briefing_id=briefing_id,
                item_count=11,
            ),
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
