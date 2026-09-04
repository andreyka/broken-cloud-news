from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch
from uuid import uuid4

import pytest

from bcn.services.distributor.service import ChannelDeliveryResult
from bcn.services.distributor.service import DeliveryResult
from bcn.common.config import Settings
from bcn.workflows.distribution import execute_distribution


def _make_settings(**overrides) -> Settings:
    defaults = {
        "database_url": "postgresql://test:test@localhost:5432/test",
        "llm_base_url": "http://fake-llm:8000/v1",
        "llm_model": "test-model",
        "comfyui_url": "http://fake-comfy:8188",
        "distribution_retry_max_attempts": 3,
        "distribution_retry_base_delay_seconds": 60,
        "distribution_retry_max_delay_seconds": 600,
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.mark.asyncio
async def test_execute_distribution_persists_attempts_and_marks_success():
    settings = _make_settings()
    briefing_id = uuid4()
    item_id = uuid4()
    distributor = AsyncMock()
    distributor.deliver.return_value = DeliveryResult(
        mode="regular_daily_briefing",
        results={"telegram": "ok", "discord": "ok"},
        attempts=(
            ChannelDeliveryResult(
                channel="discord",
                status="ok",
                external_message_id="42",
                metadata={"primary_message_id": "42"},
            ),
        ),
        all_ok=True,
        message="Distributed to: {'telegram': 'ok', 'discord': 'ok'} (mode=regular_daily_briefing)",
    )

    with (
        patch("bcn.workflows.distribution.get_pool", new_callable=AsyncMock),
        patch(
            "bcn.workflows.distribution.claim_draft_briefing_by_id",
            new_callable=AsyncMock,
            return_value={
                "id": briefing_id,
                "created_at": datetime.now(timezone.utc),
                "content_markdown": "**Draft**",
                "content_html": "<p>Draft</p>",
                "cover_image_url": "",
                "item_ids": [item_id],
            },
        ),
        patch(
            "bcn.workflows.distribution.get_distribution_outcomes",
            new_callable=AsyncMock,
            return_value=[{"channel": "telegram", "status": "ok"}],
        ),
        patch(
            "bcn.workflows.distribution.upsert_distribution_outcome",
            new_callable=AsyncMock,
        ) as mock_upsert,
        patch(
            "bcn.workflows.distribution.mark_briefing_distributed",
            new_callable=AsyncMock,
        ) as mock_mark,
        patch(
            "bcn.workflows.distribution.mark_items_published",
            new_callable=AsyncMock,
        ) as mock_publish,
        patch(
            "bcn.workflows.distribution.release_briefing_for_retry",
            new_callable=AsyncMock,
        ) as mock_release,
    ):
        result = await execute_distribution(
            settings,
            mode="regular_daily_briefing",
            briefing_id=briefing_id,
            distributor_service=distributor,
            manage_pool=False,
        )

    assert "Distributed to:" in result
    distributor.deliver.assert_awaited_once()
    request = distributor.deliver.await_args.args[0]
    assert request.mode == "regular_daily_briefing"
    assert request.briefing["id"] == briefing_id
    assert request.previous_ok_channels == frozenset({"telegram"})
    mock_upsert.assert_awaited_once_with(
        briefing_id=briefing_id,
        channel="discord",
        status="ok",
        external_message_id="42",
        metrics={},
        metadata={"primary_message_id": "42"},
    )
    mock_mark.assert_awaited_once_with(
        briefing_id,
        {"telegram": "ok", "discord": "ok"},
    )
    mock_publish.assert_awaited_once_with([item_id])
    mock_release.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_distribution_enqueues_auto_ai_review_when_configured():
    settings = _make_settings(ai_review_api_key="test-key", ai_review_auto_enabled=True)
    briefing_id = uuid4()
    distributor = AsyncMock()
    distributor.deliver.return_value = DeliveryResult(
        mode="regular_daily_briefing",
        results={"telegram": "ok"},
        attempts=(
            ChannelDeliveryResult(
                channel="telegram",
                status="ok",
                external_message_id="99",
                metadata={},
            ),
        ),
        all_ok=True,
        message="Distributed to: {'telegram': 'ok'} (mode=regular_daily_briefing)",
    )

    with (
        patch("bcn.workflows.distribution.get_pool", new_callable=AsyncMock),
        patch(
            "bcn.workflows.distribution.claim_latest_draft_briefing",
            new_callable=AsyncMock,
            return_value={
                "id": briefing_id,
                "created_at": datetime.now(timezone.utc),
                "content_markdown": "**Draft**",
                "content_html": "<p>Draft</p>",
                "cover_image_url": "",
                "item_ids": [],
            },
        ),
        patch(
            "bcn.workflows.distribution.get_distribution_outcomes",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "bcn.workflows.distribution.upsert_distribution_outcome",
            new_callable=AsyncMock,
        ),
        patch(
            "bcn.workflows.distribution.mark_briefing_distributed",
            new_callable=AsyncMock,
        ),
        patch(
            "bcn.workflows.distribution.mark_items_published",
            new_callable=AsyncMock,
        ),
        patch(
            "bcn.workflows.distribution.release_briefing_for_retry",
            new_callable=AsyncMock,
        ),
        patch(
            "bcn.workflows.ai_review.get_ai_review_config",
            return_value=SimpleNamespace(enabled=True),
        ),
        patch(
            "bcn.workflows.queue.enqueue_ai_review_job",
            new_callable=AsyncMock,
        ) as mock_enqueue_review,
    ):
        await execute_distribution(
            settings,
            mode="regular_daily_briefing",
            distributor_service=distributor,
            manage_pool=False,
        )

    mock_enqueue_review.assert_awaited_once()
    assert mock_enqueue_review.await_args.kwargs["briefing_id"] == briefing_id
    assert mock_enqueue_review.await_args.kwargs["source"] == "auto_distribution"


@pytest.mark.asyncio
async def test_execute_distribution_releases_for_retry_when_delivery_incomplete():
    settings = _make_settings()
    briefing_id = uuid4()
    distributor = AsyncMock()
    distributor.deliver.return_value = DeliveryResult(
        mode="regular_daily_briefing",
        results={"telegram": "failed"},
        attempts=(
            ChannelDeliveryResult(
                channel="telegram",
                status="failed",
                external_message_id=None,
                metadata={},
            ),
        ),
        all_ok=False,
        message=(
            "Distribution incomplete; kept briefing as DRAFT for retry. "
            "mode=regular_daily_briefing results={'telegram': 'failed'}"
        ),
    )

    with (
        patch("bcn.workflows.distribution.get_pool", new_callable=AsyncMock),
        patch(
            "bcn.workflows.distribution.claim_latest_draft_briefing",
            new_callable=AsyncMock,
            return_value={
                "id": briefing_id,
                "created_at": datetime.now(timezone.utc),
                "content_markdown": "**Draft**",
                "content_html": "<p>Draft</p>",
                "cover_image_url": "",
                "item_ids": [],
            },
        ),
        patch(
            "bcn.workflows.distribution.get_distribution_outcomes",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "bcn.workflows.distribution.upsert_distribution_outcome",
            new_callable=AsyncMock,
        ) as mock_upsert,
        patch(
            "bcn.workflows.distribution.mark_briefing_distributed",
            new_callable=AsyncMock,
        ) as mock_mark,
        patch(
            "bcn.workflows.distribution.mark_items_published",
            new_callable=AsyncMock,
        ) as mock_publish,
        patch(
            "bcn.workflows.distribution.release_briefing_for_retry",
            new_callable=AsyncMock,
        ) as mock_release,
    ):
        result = await execute_distribution(
            settings,
            mode="regular_daily_briefing",
            distributor_service=distributor,
            manage_pool=False,
        )

    assert "incomplete" in result.lower()
    mock_upsert.assert_awaited_once()
    mock_mark.assert_not_awaited()
    mock_publish.assert_not_awaited()
    mock_release.assert_awaited_once()
    assert mock_release.await_args.args == (briefing_id,)
    assert mock_release.await_args.kwargs["error"] == "distribution_incomplete:telegram"


@pytest.mark.asyncio
async def test_execute_distribution_returns_requested_briefing_unavailable():
    settings = _make_settings()
    briefing_id = uuid4()

    with (
        patch("bcn.workflows.distribution.get_pool", new_callable=AsyncMock),
        patch(
            "bcn.workflows.distribution.claim_draft_briefing_by_id",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        result = await execute_distribution(
            settings,
            mode="regular_daily_briefing",
            briefing_id=briefing_id,
            manage_pool=False,
        )

    assert str(briefing_id) in result
    assert "not available for distribution" in result


def _claimed(created_at):
    return {
        "id": uuid4(),
        "created_at": created_at,
        "content_markdown": "**Draft**",
        "content_html": "<p>Draft</p>",
        "cover_image_url": "",
        "item_ids": [],
    }


def _ok_delivery(mode: str) -> DeliveryResult:
    return DeliveryResult(
        mode=mode,
        results={"substack": "ok"},
        attempts=(
            ChannelDeliveryResult(
                channel="substack", status="ok", external_message_id="1", metadata={}
            ),
        ),
        all_ok=True,
        message=f"Distributed (mode={mode})",
    )


@pytest.mark.asyncio
async def test_execute_distribution_delivers_in_the_briefings_generated_mode():
    """A monthly draft claimed by a daily trigger still ships as a monthly edition."""
    settings = _make_settings()
    distributor = AsyncMock()
    distributor.deliver.return_value = _ok_delivery("regular_monthly_newsletter")
    claimed = _claimed(datetime.now(timezone.utc) - timedelta(hours=4))

    with (
        patch("bcn.workflows.distribution.get_pool", new_callable=AsyncMock),
        patch(
            "bcn.workflows.distribution.claim_latest_draft_briefing",
            new_callable=AsyncMock,
            return_value=claimed,
        ),
        patch(
            "bcn.workflows.distribution.get_latest_generation_run_for_briefing",
            new_callable=AsyncMock,
            return_value={"mode": "monthly_newsletter"},
        ),
        patch(
            "bcn.workflows.distribution.get_newsletter_subscribers",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "bcn.workflows.distribution.get_distribution_outcomes",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("bcn.workflows.distribution.upsert_distribution_outcome", new_callable=AsyncMock),
        patch(
            "bcn.workflows.distribution.mark_briefing_distributed", new_callable=AsyncMock
        ) as mock_mark,
        patch("bcn.workflows.distribution.mark_items_published", new_callable=AsyncMock),
        patch("bcn.workflows.distribution.mark_briefing_stale", new_callable=AsyncMock) as mock_stale,
        patch("bcn.workflows.distribution.release_briefing_for_retry", new_callable=AsyncMock),
    ):
        await execute_distribution(
            settings,
            mode="regular_daily_briefing",
            distributor_service=distributor,
            manage_pool=False,
        )

    request = distributor.deliver.await_args.args[0]
    assert request.mode == "regular_monthly_newsletter"
    mock_mark.assert_awaited_once()
    mock_stale.assert_not_awaited()  # four hours is fresh for a newsletter edition


@pytest.mark.asyncio
async def test_execute_distribution_retires_stale_unattended_daily_draft():
    settings = _make_settings(briefing_distribution_max_draft_age_minutes=180)
    distributor = AsyncMock()
    claimed = _claimed(datetime.now(timezone.utc) - timedelta(hours=4))

    with (
        patch("bcn.workflows.distribution.get_pool", new_callable=AsyncMock),
        patch(
            "bcn.workflows.distribution.claim_latest_draft_briefing",
            new_callable=AsyncMock,
            return_value=claimed,
        ),
        patch(
            "bcn.workflows.distribution.get_latest_generation_run_for_briefing",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("bcn.workflows.distribution.mark_briefing_stale", new_callable=AsyncMock) as mock_stale,
        patch("bcn.workflows.distribution.send_operator_alert", new_callable=AsyncMock) as mock_alert,
        patch(
            "bcn.workflows.distribution.release_briefing_for_retry", new_callable=AsyncMock
        ) as mock_release,
    ):
        result = await execute_distribution(
            settings,
            mode="regular_daily_briefing",
            distributor_service=distributor,
            manage_pool=False,
        )

    assert "stale" in result
    mock_stale.assert_awaited_once()
    assert mock_stale.await_args.args[0] == claimed["id"]
    mock_alert.assert_awaited_once()
    mock_release.assert_not_awaited()
    distributor.deliver.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_distribution_skips_stale_guard_for_explicitly_targeted_draft():
    settings = _make_settings(briefing_distribution_max_draft_age_minutes=180)
    distributor = AsyncMock()
    distributor.deliver.return_value = _ok_delivery("regular_daily_briefing")
    claimed = _claimed(datetime.now(timezone.utc) - timedelta(days=3))

    with (
        patch("bcn.workflows.distribution.get_pool", new_callable=AsyncMock),
        patch(
            "bcn.workflows.distribution.claim_draft_briefing_by_id",
            new_callable=AsyncMock,
            return_value=claimed,
        ),
        patch(
            "bcn.workflows.distribution.get_latest_generation_run_for_briefing",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "bcn.workflows.distribution.get_distribution_outcomes",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("bcn.workflows.distribution.upsert_distribution_outcome", new_callable=AsyncMock),
        patch("bcn.workflows.distribution.mark_briefing_distributed", new_callable=AsyncMock),
        patch("bcn.workflows.distribution.mark_items_published", new_callable=AsyncMock),
        patch("bcn.workflows.distribution.mark_briefing_stale", new_callable=AsyncMock) as mock_stale,
        patch("bcn.workflows.distribution.release_briefing_for_retry", new_callable=AsyncMock),
    ):
        await execute_distribution(
            settings,
            mode="regular_daily_briefing",
            briefing_id=claimed["id"],
            distributor_service=distributor,
            manage_pool=False,
        )

    distributor.deliver.assert_awaited_once()
    mock_stale.assert_not_awaited()
