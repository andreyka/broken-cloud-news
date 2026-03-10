from __future__ import annotations

import json
from unittest.mock import AsyncMock
from unittest.mock import patch
from uuid import uuid4

import pytest

from bcn.common.config import Settings
from bcn.contracts.review import CritiqueRequest
from bcn.contracts.review import VerificationRequest
from bcn.contracts.review import render_critique_request_payload
from bcn.contracts.review import render_verification_request_payload
from bcn.services.critic.service import CriticService
from bcn.workflows.review import execute_critique
from bcn.workflows.review import execute_verification


def _make_settings(**overrides) -> Settings:
    defaults = {
        "database_url": "postgresql://test:test@localhost:5432/test",
        "llm_base_url": "http://fake-llm:8000/v1",
        "llm_model": "test-model",
        "comfyui_url": "http://fake-comfy:8188",
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.mark.asyncio
async def test_execute_critique_loads_latest_briefing_from_control_plane():
    settings = _make_settings()
    briefing_id = uuid4()
    service = AsyncMock()
    service.evaluate.return_value = {
        "source": f"briefing:{briefing_id}",
        "gate_passed": True,
        "critic_passed": True,
        "critic_score": 91,
        "critic_dimension_scores": {"actionability": 90},
        "threshold_passed": True,
        "thresholds": {"min_score": 80},
        "gate_issues": [],
        "critic_issues": [],
        "recommendations": [],
    }

    with (
        patch("bcn.workflows.review.get_pool", new_callable=AsyncMock),
        patch(
            "bcn.workflows.review.get_latest_any_briefing",
            new_callable=AsyncMock,
            return_value={
                "id": briefing_id,
                "content_markdown": "**Draft**",
                "item_ids": [uuid4()],
            },
        ),
        patch(
            "bcn.workflows.review.get_items_by_ids",
            new_callable=AsyncMock,
            return_value=[{"url": "https://example.com/one", "title": "One"}],
        ),
    ):
        result = await execute_critique(
            settings,
            latest=True,
            critic_service=service,
            source="cli",
            manage_pool=False,
        )

    service.evaluate.assert_awaited_once()
    request = service.evaluate.await_args.args[0]
    assert request.draft_markdown == "**Draft**"
    assert request.source == f"briefing:{briefing_id}"
    assert list(request.items) == [{"url": "https://example.com/one", "title": "One"}]
    payload = json.loads(result)
    assert payload["critic_score"] == 91


@pytest.mark.asyncio
async def test_execute_verification_with_explicit_markdown_skips_latest_lookup():
    settings = _make_settings()
    service = AsyncMock()
    service.evaluate.return_value = {
        "source": "cli",
        "verifier_passed": True,
        "verifier_score": 95,
        "issues": [],
        "recommendations": [],
        "dead_urls": [],
        "top_story_ok": True,
    }

    with (
        patch("bcn.workflows.review.get_pool", new_callable=AsyncMock),
        patch(
            "bcn.workflows.review.get_latest_any_briefing",
            new_callable=AsyncMock,
        ) as mock_latest,
    ):
        result = await execute_verification(
            settings,
            markdown="**Explicit**",
            verifier_service=service,
            source="cli",
            manage_pool=False,
        )

    mock_latest.assert_not_awaited()
    service.evaluate.assert_awaited_once()
    request = service.evaluate.await_args.args[0]
    assert request.draft_markdown == "**Explicit**"
    assert request.source == "cli"
    payload = json.loads(result)
    assert payload["verifier_score"] == 95


@pytest.mark.asyncio
async def test_critic_service_reports_threshold_failures_in_result_and_logs(caplog):
    settings = _make_settings(
        briefing_critic_min_score=80,
        briefing_critic_min_actionability=70,
        briefing_critic_min_link_hygiene=80,
    )
    service = CriticService(settings, llm_client=AsyncMock())
    service.critic_llm = AsyncMock()
    service.critic_llm.critique_briefing.return_value = {
        "passed": True,
        "score": 96,
        "dimension_scores": {"actionability": 60, "link_hygiene": 92},
        "issues": ["Needs stronger remediation"],
        "recommendations": ["Be more explicit"],
    }
    request = CritiqueRequest(
        source="briefing:test",
        draft_markdown="**Draft**",
        items=[{"url": "https://example.com/one", "title": "One"}],
        mode="regular_daily_briefing",
    )

    with caplog.at_level("INFO"):
        result = await service.evaluate(request)

    assert result["critic_passed"] is True
    assert result["threshold_passed"] is False
    assert result["threshold_failures"] == {
        "actionability": {"actual": 60, "required": 70}
    }
    assert "threshold=False" in caplog.text
    assert "actionability" in caplog.text
