from __future__ import annotations

from datetime import datetime
from datetime import timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import click
from click.testing import CliRunner

import bcn.cli as cli_module
from bcn.common.config import Settings
from bcn.workflows.automation import build_regular_briefing_trigger
from bcn.workflows.automation import build_regular_monthly_newsletter_trigger
from bcn.workflows.automation import build_shadow_regular_briefing_trigger
from bcn.workflows.automation import configure_scheduler_runtime
from bcn.workflows.automation import extract_briefing_id
from bcn.workflows.automation import job_shadow_regular_briefing
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


def test_build_shadow_regular_briefing_trigger_offsets_multi_hour_schedule():
    settings = Settings(
        distribute_hours=[19, 9, 13],
        distribute_minute=0,
        distribute_timezone="UTC",
        shadow_minutes_before_publish=45,
    )
    trigger = build_shadow_regular_briefing_trigger(settings)
    assert trigger.hour == "8,12,18"
    assert trigger.minute == 15
    assert str(trigger.timezone) == "UTC"


def test_build_shadow_regular_briefing_trigger_wraps_before_midnight():
    settings = Settings(
        distribute_hour=0,
        distribute_hours=[],
        distribute_minute=15,
        distribute_timezone="UTC",
        shadow_minutes_before_publish=45,
    )
    trigger = build_shadow_regular_briefing_trigger(settings)
    assert trigger.hour == "23"
    assert trigger.minute == 30
    assert str(trigger.timezone) == "UTC"


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


def test_extract_text_from_rpc_result_supports_message_parts():
    handoff = (
        'writer_handoff::{"briefing_id":"123e4567-e89b-12d3-a456-426614174000",'
        '"decision":"publish","item_count":5,"mode":"regular_daily_briefing"}'
    )
    payload = {
        "jsonrpc": "2.0",
        "id": "abc",
        "result": {
            "kind": "message",
            "messageId": "msg-1",
            "parts": [{"kind": "text", "text": f"{handoff}\nBriefing created"}],
            "role": "agent",
        },
    }

    assert cli_module._extract_text_from_rpc_result(payload) == (
        f"{handoff}\nBriefing created"
    )


def test_extract_text_from_rpc_result_keeps_artifact_compatibility():
    payload = {
        "jsonrpc": "2.0",
        "id": "abc",
        "result": {
            "artifacts": [{"parts": [{"kind": "text", "text": "artifact-text"}]}],
        },
    }

    assert cli_module._extract_text_from_rpc_result(payload) == "artifact-text"


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
    run_distribution = AsyncMock(return_value="Distributed to: {'telegram': 'ok'}")

    writer_result, distributor_result = await run_writer_distributor_handoff(
        mode=REGULAR_DAILY_BRIEFING_MODE,
        run_writer=run_writer,
        run_distribution=run_distribution,
    )

    assert "writer_handoff::" in writer_result
    assert "Distributed to:" in str(distributor_result)
    run_writer.assert_awaited_once_with(
        f"generate_briefing::{REGULAR_DAILY_BRIEFING_MODE}"
    )
    run_distribution.assert_awaited_once_with(
        REGULAR_DAILY_BRIEFING_MODE,
        briefing_id,
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
    run_distribution = AsyncMock(return_value="Distributed to: {'telegram': 'ok'}")

    _writer_result, distributor_result = await run_writer_distributor_handoff(
        mode=REGULAR_DAILY_BRIEFING_MODE,
        run_writer=run_writer,
        run_distribution=run_distribution,
    )

    assert "Distributed to:" in str(distributor_result)
    run_distribution.assert_awaited_once_with(
        REGULAR_DAILY_BRIEFING_MODE,
        briefing_id,
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
    run_distribution = AsyncMock(return_value="unused")

    _writer_result, distributor_result = await run_writer_distributor_handoff(
        mode=REGULAR_DAILY_BRIEFING_MODE,
        run_writer=run_writer,
        run_distribution=run_distribution,
    )

    assert distributor_result is None
    run_writer.assert_awaited_once_with(
        f"generate_briefing::{REGULAR_DAILY_BRIEFING_MODE}"
    )
    run_distribution.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_writer_distributor_handoff_skips_unstructured_writer_output():
    briefing_id = uuid4()
    run_writer = AsyncMock(return_value=f"Briefing created: id={briefing_id} items=3")
    run_distribution = AsyncMock(return_value="unused")

    _writer_result, distributor_result = await run_writer_distributor_handoff(
        mode=REGULAR_DAILY_BRIEFING_MODE,
        run_writer=run_writer,
        run_distribution=run_distribution,
    )

    assert distributor_result is None
    run_writer.assert_awaited_once_with(
        f"generate_briefing::{REGULAR_DAILY_BRIEFING_MODE}"
    )
    run_distribution.assert_not_awaited()


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
async def test_job_publish_regular_briefing_distributes_target_briefing(monkeypatch):
    settings = Settings(writer_port=9003, distributor_port=9004)
    briefing_id = uuid4()
    send_mock = AsyncMock(
        return_value=render_writer_handoff_payload(
            mode=REGULAR_DAILY_BRIEFING_MODE,
            decision="publish",
            briefing_id=briefing_id,
            item_count=3,
        )
    )
    distribute_mock = AsyncMock(return_value="Distributed to: {'telegram': 'ok'}")
    monkeypatch.setattr("bcn.workflows.modes.common.execute_distribution", distribute_mock)
    configure_scheduler_runtime(settings, send_mock)

    await job_publish_regular_briefing()

    assert send_mock.await_count == 1
    assert (
        send_mock.await_args_list[0].args
        == (9003, f"generate_briefing::{REGULAR_DAILY_BRIEFING_MODE}")
    )
    distribute_mock.assert_awaited_once_with(
        settings,
        mode=REGULAR_DAILY_BRIEFING_MODE,
        briefing_id=briefing_id,
        manage_pool=False,
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
async def test_job_publish_regular_monthly_newsletter_uses_monthly_mode(monkeypatch):
    settings = Settings(writer_port=9003, distributor_port=9004)
    briefing_id = uuid4()
    send_mock = AsyncMock(
        return_value=render_writer_handoff_payload(
            mode=REGULAR_MONTHLY_NEWSLETTER_MODE,
            decision="publish",
            briefing_id=briefing_id,
            item_count=11,
        )
    )
    distribute_mock = AsyncMock(return_value="Distributed to: {'email': 'ok'}")
    monkeypatch.setattr("bcn.workflows.modes.common.execute_distribution", distribute_mock)
    configure_scheduler_runtime(settings, send_mock)

    await job_publish_regular_monthly_newsletter()

    assert send_mock.await_count == 1
    assert (
        send_mock.await_args_list[0].args
        == (9003, f"generate_briefing::{REGULAR_MONTHLY_NEWSLETTER_MODE}")
    )
    distribute_mock.assert_awaited_once_with(
        settings,
        mode=REGULAR_MONTHLY_NEWSLETTER_MODE,
        briefing_id=briefing_id,
        manage_pool=False,
    )


@pytest.mark.asyncio
async def test_job_shadow_regular_briefing_persists_report(monkeypatch, tmp_path):
    overrides_path = tmp_path / "candidate.json"
    overrides_path.write_text('{"llm_model_writer":"candidate"}', encoding="utf-8")
    settings = Settings(
        shadow_enabled=True,
        shadow_candidate_overrides_path=str(overrides_path),
        shadow_include_text=True,
    )
    configure_scheduler_runtime(settings, AsyncMock())

    run_mock = AsyncMock(
        return_value={
            "lane": "shadow",
            "db_run_id": "shadow-run-id",
            "item_pool_count": 5,
            "summary": {"recommendation": "promote", "confidence": "medium"},
        }
    )
    monkeypatch.setattr("bcn.evaluation.service.execute_shadow_lane", run_mock)

    await job_shadow_regular_briefing()

    run_mock.assert_awaited_once_with(
        settings,
        workflow_mode=REGULAR_DAILY_BRIEFING_MODE,
        candidate_overrides_path=str(overrides_path),
        output_path=None,
        include_text=True,
        store_db=True,
        source="scheduler",
        notes="Scheduled pre-publish shadow evaluation.",
        manage_pool=False,
    )


def test_distribute_command_delegates_to_distribution_service(monkeypatch):
    runner = CliRunner()
    target_id = uuid4()
    run_mock = AsyncMock(return_value="Distributed to: {'telegram': 'ok'}")
    monkeypatch.setattr(cli_module, "execute_distribution", run_mock)

    result = runner.invoke(
        cli_module.cli,
        ["distribute", "--briefing-id", str(target_id)],
    )

    assert result.exit_code == 0
    assert "Distributed to:" in result.output
    assert run_mock.await_count == 1
    assert isinstance(run_mock.await_args.args[0], Settings)
    assert run_mock.await_args.kwargs == {
        "mode": REGULAR_DAILY_BRIEFING_MODE,
        "briefing_id": target_id,
    }


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


def test_workflow_run_command_delegates_to_workflow_service(monkeypatch):
    runner = CliRunner()
    execute_mock = AsyncMock(
        return_value=("writer_handoff::{}", "Distributed to: {'telegram': 'ok'}")
    )
    monkeypatch.setattr(cli_module, "execute_workflow_mode", execute_mock)

    result = runner.invoke(
        cli_module.cli,
        ["workflow-run", "--mode", REGULAR_DAILY_BRIEFING_MODE],
    )

    assert result.exit_code == 0
    assert "writer_handoff::" in result.output
    assert "Distributed to:" in result.output
    assert execute_mock.await_count == 1
    assert execute_mock.await_args.kwargs["mode"] == REGULAR_DAILY_BRIEFING_MODE
    assert execute_mock.await_args.kwargs["agent_client"] is not None


def test_run_command_delegates_to_workflow_daemon_service(monkeypatch):
    runner = CliRunner()
    daemon_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(cli_module, "run_daemon", daemon_mock)

    result = runner.invoke(cli_module.cli, ["run"])

    assert result.exit_code == 0
    assert daemon_mock.await_count == 1
    settings_arg = daemon_mock.await_args.args[0]
    assert isinstance(settings_arg, Settings)
    assert daemon_mock.await_args.kwargs["emit"] is click.echo
    assert daemon_mock.await_args.kwargs["agent_client"] is not None
