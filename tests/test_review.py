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


@pytest.mark.asyncio
async def test_critic_service_honours_writer_length_limits():
    """A draft longer than the mode ceiling must not be flagged when the writer's
    computed (item-scaled) ceiling is supplied with the request."""
    settings = _make_settings(briefing_min_chars=1200, briefing_hard_max_chars=2300)
    service = CriticService(settings, llm_client=AsyncMock())
    service.critic_llm = AsyncMock()
    service.critic_llm.critique_briefing.return_value = {
        "passed": True,
        "score": 90,
        "dimension_scores": {},
        "issues": [],
        "recommendations": [],
    }
    draft = "**Story**\n\n" + ("Sentence with a fact. " * 120) + "[ref](https://example.com/one)"
    assert len(draft) > 2300
    items = [{"url": "https://example.com/one", "title": "One"}]

    scaled = await service.evaluate(
        CritiqueRequest(draft_markdown=draft, items=items, mode="standard", hard_max_chars=3100)
    )
    assert scaled["gate_passed"] is True
    sent = service.critic_llm.critique_briefing.await_args.kwargs
    assert not any("too long" in issue for issue in sent["gate_hard_issues"])

    unscaled = await service.evaluate(
        CritiqueRequest(draft_markdown=draft, items=items, mode="standard")
    )
    assert unscaled["gate_passed"] is False
    sent = service.critic_llm.critique_briefing.await_args.kwargs
    assert any("too long" in issue for issue in sent["gate_hard_issues"])


def test_critique_request_payload_round_trips_length_limits():
    from bcn.contracts.review import critique_request_from_payload
    from bcn.contracts.review import critique_request_to_payload

    request = CritiqueRequest(
        draft_markdown="**Draft**", mode="standard", min_chars=1200, hard_max_chars=3100
    )
    parsed = critique_request_from_payload(critique_request_to_payload(request))
    assert parsed is not None
    assert (parsed.min_chars, parsed.hard_max_chars) == (1200, 3100)
    bare = critique_request_from_payload({"draft_markdown": "**Draft**"})
    assert bare is not None
    assert (bare.min_chars, bare.hard_max_chars) == (None, None)


@pytest.mark.asyncio
async def test_writer_critique_markdown_forwards_length_limits():
    from types import SimpleNamespace

    from bcn.services.writer.review import critique_markdown

    evaluator = AsyncMock()
    evaluator.evaluate.return_value = {"critic_passed": True, "critic_score": 90}
    service = SimpleNamespace(
        settings=SimpleNamespace(briefing_critique_enabled=True),
        critic_evaluator=evaluator,
    )
    await critique_markdown(
        service, "**Draft**", [], mode="standard", min_chars=1200, hard_max_chars=3100
    )
    sent = evaluator.evaluate.await_args.args[0]
    assert (sent.min_chars, sent.hard_max_chars) == (1200, 3100)


def test_critic_service_threshold_failures_respect_score_margin():
    settings = _make_settings(
        briefing_critic_min_score=80, briefing_critic_score_override_margin=5
    )
    service = CriticService(settings, llm_client=AsyncMock())
    base = {"dimension_scores": {"actionability": 90, "link_hygiene": 90}, "passed": False}
    assert "critic_passed" not in service._threshold_failures({**base, "score": 87})
    assert "critic_passed" in service._threshold_failures({**base, "score": 83})
