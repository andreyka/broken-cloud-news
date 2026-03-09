from __future__ import annotations

from datetime import datetime
from datetime import timezone
from unittest.mock import AsyncMock
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

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
from bcn.workflows.automation import job_analyze_items
from bcn.workflows.automation import job_collect_ghsa
from bcn.workflows.automation import job_collect_reddit
from bcn.workflows.automation import job_collect_rss
from bcn.workflows.automation import job_collect_twitter
from bcn.workflows.automation import job_shadow_regular_briefing
from bcn.workflows.automation import job_publish_regular_monthly_newsletter
from bcn.workflows.automation import job_publish_regular_briefing
from bcn.workflows.catalog import iter_scheduled_workflows
from bcn.workflows.modes import REGULAR_DAILY_BRIEFING_MODE
from bcn.workflows.modes import REGULAR_MONTHLY_NEWSLETTER_MODE
from bcn.workflows.modes.common import parse_writer_handoff_payload
from bcn.workflows.modes.common import WriterHandoff
from bcn.workflows.modes.common import WriterHandoffResult
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


def test_build_regular_briefing_trigger_preserves_first_local_slot(monkeypatch):
    start = datetime(2026, 3, 7, 2, 0, 6, 320330, tzinfo=ZoneInfo("America/Los_Angeles"))
    monkeypatch.setattr(
        "bcn.workflows.modes.regular_daily_briefing.schedule_start_time",
        lambda timezone_name: start,
    )

    settings = Settings(
        distribute_hours=[9, 13, 19],
        distribute_minute=0,
        distribute_timezone="America/Los_Angeles",
    )
    trigger = build_regular_briefing_trigger(settings)

    assert trigger.start_time == start
    assert trigger.next() == datetime(2026, 3, 7, 9, 0, tzinfo=ZoneInfo("America/Los_Angeles"))


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


def test_build_shadow_regular_briefing_trigger_preserves_first_local_slot(monkeypatch):
    start = datetime(2026, 3, 7, 2, 0, 6, 320330, tzinfo=ZoneInfo("America/Los_Angeles"))
    monkeypatch.setattr(
        "bcn.workflows.modes.regular_daily_briefing.schedule_start_time",
        lambda timezone_name: start,
    )

    settings = Settings(
        distribute_hours=[9, 13, 19],
        distribute_minute=0,
        distribute_timezone="America/Los_Angeles",
        shadow_minutes_before_publish=45,
    )
    trigger = build_shadow_regular_briefing_trigger(settings)

    assert trigger.start_time == start
    assert trigger.next() == datetime(2026, 3, 7, 8, 15, tzinfo=ZoneInfo("America/Los_Angeles"))


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


@pytest.mark.asyncio
async def test_run_writer_distributor_handoff_uses_shared_skill_format():
    briefing_id = uuid4()
    run_generation = AsyncMock(
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
        run_generation=run_generation,
        run_distribution=run_distribution,
    )

    assert "writer_handoff::" in writer_result
    assert "Distributed to:" in str(distributor_result)
    run_generation.assert_awaited_once_with(REGULAR_DAILY_BRIEFING_MODE)
    run_distribution.assert_awaited_once_with(
        REGULAR_DAILY_BRIEFING_MODE,
        briefing_id,
    )


@pytest.mark.asyncio
async def test_run_writer_distributor_handoff_enforces_requested_mode():
    briefing_id = uuid4()
    run_generation = AsyncMock(
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
        run_generation=run_generation,
        run_distribution=run_distribution,
    )

    assert "Distributed to:" in str(distributor_result)
    run_distribution.assert_awaited_once_with(
        REGULAR_DAILY_BRIEFING_MODE,
        briefing_id,
    )


@pytest.mark.asyncio
async def test_run_writer_distributor_handoff_accepts_typed_result():
    briefing_id = uuid4()
    run_generation = AsyncMock(
        return_value=WriterHandoffResult(
            handoff=WriterHandoff(
                mode=REGULAR_DAILY_BRIEFING_MODE,
                decision="publish",
                briefing_id=briefing_id,
                item_count=2,
            ),
            human_message="Briefing created",
        )
    )
    run_distribution = AsyncMock(return_value="Distributed to: {'telegram': 'ok'}")

    writer_result, distributor_result = await run_writer_distributor_handoff(
        mode=REGULAR_DAILY_BRIEFING_MODE,
        run_generation=run_generation,
        run_distribution=run_distribution,
    )

    assert "writer_handoff::" in writer_result
    assert "Briefing created" in writer_result
    assert "Distributed to:" in str(distributor_result)
    run_distribution.assert_awaited_once_with(
        REGULAR_DAILY_BRIEFING_MODE,
        briefing_id,
    )


@pytest.mark.asyncio
async def test_run_writer_distributor_handoff_skips_distribution_on_skip_decision():
    run_generation = AsyncMock(
        return_value=render_writer_handoff_payload(
            mode=REGULAR_DAILY_BRIEFING_MODE,
            decision="skip",
            item_count=0,
        )
    )
    run_distribution = AsyncMock(return_value="unused")

    _writer_result, distributor_result = await run_writer_distributor_handoff(
        mode=REGULAR_DAILY_BRIEFING_MODE,
        run_generation=run_generation,
        run_distribution=run_distribution,
    )

    assert distributor_result is None
    run_generation.assert_awaited_once_with(REGULAR_DAILY_BRIEFING_MODE)
    run_distribution.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_writer_distributor_handoff_skips_unstructured_writer_output():
    briefing_id = uuid4()
    run_generation = AsyncMock(
        return_value=f"Briefing created: id={briefing_id} items=3"
    )
    run_distribution = AsyncMock(return_value="unused")

    _writer_result, distributor_result = await run_writer_distributor_handoff(
        mode=REGULAR_DAILY_BRIEFING_MODE,
        run_generation=run_generation,
        run_distribution=run_distribution,
    )

    assert distributor_result is None
    run_generation.assert_awaited_once_with(REGULAR_DAILY_BRIEFING_MODE)
    run_distribution.assert_not_awaited()


def test_build_regular_monthly_newsletter_trigger():
    start = datetime(2026, 3, 1, 2, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    with patch(
        "bcn.workflows.modes.regular_monthly_newsletter.schedule_start_time",
        return_value=start,
    ):
        settings = Settings(
            monthly_newsletter_day=3,
            monthly_newsletter_hour=17,
            monthly_newsletter_minute=5,
            monthly_newsletter_timezone="America/Los_Angeles",
        )
        trigger = build_regular_monthly_newsletter_trigger(settings)

    assert trigger.start_time == start
    assert trigger.day == 3
    assert trigger.hour == 17
    assert trigger.minute == 5
    assert str(trigger.timezone) == "America/Los_Angeles"


def test_iter_scheduled_workflows_reflects_enabled_optional_jobs():
    settings = Settings(
        shadow_enabled=True,
        monthly_newsletter_enabled=True,
    )

    definitions = iter_scheduled_workflows(settings)
    ids = {definition.workflow_id for definition in definitions}

    assert "ghsa_collector" in ids
    assert "rss_collector" in ids
    assert "reddit_collector" in ids
    assert "twitter_collector" in ids
    assert "analyst" in ids
    assert f"{REGULAR_DAILY_BRIEFING_MODE}_shadow" in ids
    assert REGULAR_DAILY_BRIEFING_MODE in ids
    assert REGULAR_MONTHLY_NEWSLETTER_MODE in ids

    daily = next(
        definition
        for definition in definitions
        if definition.workflow_id == REGULAR_DAILY_BRIEFING_MODE
    )
    assert [step.component for step in daily.steps] == ["writer", "distributor"]


def test_iter_scheduled_workflows_skips_disabled_optional_jobs():
    settings = Settings(
        shadow_enabled=False,
        monthly_newsletter_enabled=False,
    )

    ids = {definition.workflow_id for definition in iter_scheduled_workflows(settings)}

    assert f"{REGULAR_DAILY_BRIEFING_MODE}_shadow" not in ids
    assert REGULAR_MONTHLY_NEWSLETTER_MODE not in ids


@pytest.mark.asyncio
async def test_job_analyze_items_uses_control_plane(monkeypatch):
    settings = Settings()
    analysis_mock = AsyncMock(return_value="Analyzed 3/3 items")
    monkeypatch.setattr("bcn.workflows.automation.execute_analysis", analysis_mock)
    runtime = configure_scheduler_runtime(settings)

    await job_analyze_items(runtime)

    analysis_mock.assert_awaited_once_with(
        settings,
        source="scheduler",
        manage_pool=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("job_func", "source"),
    [
        (job_collect_ghsa, "ghsa"),
        (job_collect_rss, "rss"),
        (job_collect_twitter, "twitter"),
        (job_collect_reddit, "reddit"),
    ],
)
async def test_collect_jobs_use_control_plane(monkeypatch, job_func, source):
    settings = Settings()
    collect_mock = AsyncMock(return_value=f"{source}: ok")
    monkeypatch.setattr("bcn.workflows.automation.execute_collection", collect_mock)
    runtime = configure_scheduler_runtime(settings)

    await job_func(runtime)

    collect_mock.assert_awaited_once_with(
        settings,
        source=source,
        origin="scheduler",
        manage_pool=False,
    )


@pytest.mark.asyncio
async def test_job_publish_regular_briefing_distributes_target_briefing(monkeypatch):
    settings = Settings()
    briefing_id = uuid4()
    generation_mock = AsyncMock(
        return_value=render_writer_handoff_payload(
            mode=REGULAR_DAILY_BRIEFING_MODE,
            decision="publish",
            briefing_id=briefing_id,
            item_count=3,
        )
    )
    distribute_mock = AsyncMock(return_value="Distributed to: {'telegram': 'ok'}")
    monkeypatch.setattr(
        "bcn.workflows.generation.execute_generation_result",
        generation_mock,
    )
    monkeypatch.setattr("bcn.workflows.modes.common.execute_distribution", distribute_mock)
    runtime = configure_scheduler_runtime(settings)

    await job_publish_regular_briefing(runtime)

    generation_mock.assert_awaited_once_with(
        settings,
        mode=REGULAR_DAILY_BRIEFING_MODE,
        source="scheduler",
        manage_pool=False,
    )
    distribute_mock.assert_awaited_once_with(
        settings,
        mode=REGULAR_DAILY_BRIEFING_MODE,
        briefing_id=briefing_id,
        manage_pool=False,
    )


@pytest.mark.asyncio
async def test_job_publish_regular_briefing_skips_distribution_when_writer_skips():
    settings = Settings()
    generation_mock = AsyncMock(
        return_value=render_writer_handoff_payload(
            mode=REGULAR_DAILY_BRIEFING_MODE,
            decision="skip",
            item_count=0,
        )
    )
    with patch("bcn.workflows.generation.execute_generation_result", generation_mock):
        runtime = configure_scheduler_runtime(settings)

        await job_publish_regular_briefing(runtime)

    generation_mock.assert_awaited_once_with(
        settings,
        mode=REGULAR_DAILY_BRIEFING_MODE,
        source="scheduler",
        manage_pool=False,
    )


@pytest.mark.asyncio
async def test_job_publish_regular_monthly_newsletter_uses_monthly_mode(monkeypatch):
    settings = Settings()
    briefing_id = uuid4()
    generation_mock = AsyncMock(
        return_value=render_writer_handoff_payload(
            mode=REGULAR_MONTHLY_NEWSLETTER_MODE,
            decision="publish",
            briefing_id=briefing_id,
            item_count=11,
        )
    )
    distribute_mock = AsyncMock(return_value="Distributed to: {'email': 'ok'}")
    monkeypatch.setattr(
        "bcn.workflows.generation.execute_generation_result",
        generation_mock,
    )
    monkeypatch.setattr("bcn.workflows.modes.common.execute_distribution", distribute_mock)
    runtime = configure_scheduler_runtime(settings)

    await job_publish_regular_monthly_newsletter(runtime)

    generation_mock.assert_awaited_once_with(
        settings,
        mode=REGULAR_MONTHLY_NEWSLETTER_MODE,
        source="scheduler",
        manage_pool=False,
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
    runtime = configure_scheduler_runtime(settings)

    run_mock = AsyncMock(
        return_value={
            "lane": "shadow",
            "db_run_id": "shadow-run-id",
            "item_pool_count": 5,
            "summary": {"recommendation": "promote", "confidence": "medium"},
        }
    )
    monkeypatch.setattr("bcn.evaluation.service.execute_shadow_lane", run_mock)

    await job_shadow_regular_briefing(runtime)

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


def test_collect_command_delegates_to_collection_service(monkeypatch):
    runner = CliRunner()
    run_mock = AsyncMock(return_value="GHSA: collected 1 items")
    monkeypatch.setattr(cli_module, "execute_collection", run_mock)

    result = runner.invoke(cli_module.cli, ["collect", "--source", "ghsa"])

    assert result.exit_code == 0
    assert "GHSA: collected 1 items" in result.output
    assert run_mock.await_count == 1
    assert isinstance(run_mock.await_args.args[0], Settings)
    assert run_mock.await_args.kwargs == {
        "source": "ghsa",
        "origin": "cli",
        "manage_pool": True,
    }


def test_critique_command_delegates_to_control_plane(monkeypatch):
    runner = CliRunner()
    run_mock = AsyncMock(return_value='{"critic_score": 88}')
    monkeypatch.setattr(cli_module, "critique_briefing", run_mock)

    result = runner.invoke(cli_module.cli, ["critique", "--text", "**Draft**"])

    assert result.exit_code == 0
    assert '"critic_score": 88' in result.output
    assert run_mock.await_count == 1
    assert run_mock.await_args.kwargs == {
        "settings": run_mock.await_args.kwargs["settings"],
        "latest": False,
        "file_path": None,
        "text_input": "**Draft**",
    }
    assert isinstance(run_mock.await_args.kwargs["settings"], Settings)


def test_verify_command_delegates_to_control_plane(monkeypatch):
    runner = CliRunner()
    run_mock = AsyncMock(return_value='{"verifier_score": 92}')
    monkeypatch.setattr(cli_module, "verify_briefing", run_mock)

    result = runner.invoke(cli_module.cli, ["verify", "--text", "**Draft**"])

    assert result.exit_code == 0
    assert '"verifier_score": 92' in result.output
    assert run_mock.await_count == 1
    assert run_mock.await_args.kwargs == {
        "settings": run_mock.await_args.kwargs["settings"],
        "latest": False,
        "file_path": None,
        "text_input": "**Draft**",
    }
    assert isinstance(run_mock.await_args.kwargs["settings"], Settings)


def test_newsletter_subscribers_list_command(monkeypatch):
    runner = CliRunner()
    now = datetime.now(timezone.utc)
    monkeypatch.setattr("bcn.persistence.runtime.get_pool", AsyncMock())
    monkeypatch.setattr("bcn.persistence.runtime.close_pool", AsyncMock())
    monkeypatch.setattr(
        "bcn.persistence.newsletter.get_newsletter_subscribers",
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
    monkeypatch.setattr("bcn.persistence.runtime.get_pool", AsyncMock())
    monkeypatch.setattr("bcn.persistence.runtime.close_pool", AsyncMock())
    add_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("bcn.persistence.newsletter.add_newsletter_subscriber", add_mock)

    result = runner.invoke(
        cli_module.cli,
        ["newsletter-subscribers", "add", "alice@example.com"],
    )

    assert result.exit_code == 0
    assert "Added newsletter subscriber: alice@example.com" in result.output
    assert add_mock.await_count == 1


def test_newsletter_subscribers_remove_command(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr("bcn.persistence.runtime.get_pool", AsyncMock())
    monkeypatch.setattr("bcn.persistence.runtime.close_pool", AsyncMock())
    remove_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "bcn.persistence.newsletter.remove_newsletter_subscriber",
        remove_mock,
    )

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
    assert execute_mock.await_args.kwargs == {
        "mode": REGULAR_DAILY_BRIEFING_MODE,
    }


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
    assert daemon_mock.await_args.kwargs == {"emit": click.echo}
