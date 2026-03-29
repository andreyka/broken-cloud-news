from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from bcn.common.config import Settings
from bcn.workflows.ai_review import AIReviewResult
from bcn.workflows.ai_review import run_briefing_ai_review


def _make_settings(**overrides) -> Settings:
    defaults = {
        "database_url": "postgresql://test:test@localhost:5432/test",
        "llm_base_url": "http://fake-llm:8000/v1",
        "llm_model": "test-model",
        "comfyui_url": "http://fake-comfy:8188",
        "ai_review_api_key": "test-key",
        "ai_review_model": "gpt-5.4",
        "ai_review_auto_enabled": True,
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.mark.asyncio
async def test_run_briefing_ai_review_skips_if_already_reviewed(monkeypatch):
    monkeypatch.setattr(
        "bcn.workflows.ai_review.get_briefing_review_context",
        AsyncMock(
            return_value={
                "id": uuid4(),
                "status": "DISTRIBUTED",
                "content_markdown": "**Draft**",
                "run_rewrite_count": 1,
            }
        ),
    )
    monkeypatch.setattr(
        "bcn.workflows.ai_review.has_ai_review",
        AsyncMock(return_value=True),
    )

    result = await run_briefing_ai_review(_make_settings(), briefing_id=uuid4())

    assert result["status"] == "skipped"
    assert result["reason"] == "already_reviewed"


@pytest.mark.asyncio
async def test_run_briefing_ai_review_persists_review(monkeypatch):
    briefing_id = uuid4()
    run_id = uuid4()
    monkeypatch.setattr(
        "bcn.workflows.ai_review.get_briefing_review_context",
        AsyncMock(
            return_value={
                "id": briefing_id,
                "status": "DISTRIBUTED",
                "content_markdown": "**Draft**",
                "run_id": run_id,
                "run_llm_model": "nemotron",
                "run_decision": "PUBLISHED",
                "run_rewrite_count": 2,
            }
        ),
    )
    monkeypatch.setattr(
        "bcn.workflows.ai_review.has_ai_review",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "bcn.workflows.ai_review.run_openai_editorial_review",
        AsyncMock(
            return_value=AIReviewResult(
                reviewer_provider="openai",
                reviewer_model="gpt-5.4-2026-03-05",
                reasoning_effort="high",
                decision="edit",
                issue_tags=["unsupported_claim", "weak_cloud_focus"],
                notes="Tighten claims.",
                edited_markdown="**Edited**",
                raw_response={"extracted_review": {"decision": "edit"}},
            )
        ),
    )
    insert_mock = AsyncMock(return_value=uuid4())
    monkeypatch.setattr("bcn.workflows.ai_review.insert_ai_review", insert_mock)

    result = await run_briefing_ai_review(
        _make_settings(),
        briefing_id=briefing_id,
        source="auto_distribution",
    )

    assert result["status"] == "stored"
    assert result["decision"] == "edit"
    insert_mock.assert_awaited_once()
    assert insert_mock.await_args.kwargs["briefing_id"] == briefing_id
    assert insert_mock.await_args.kwargs["run_id"] == run_id
