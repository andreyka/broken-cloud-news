from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from bcn.common.config import Settings
from bcn.workflows.ai_review import AIReviewResult
from bcn.workflows.ai_review import run_prepublish_ai_review_gate
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
    review_mock = AsyncMock(
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
    )
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
                "run_selected_items": [
                    {
                        "title": "Axios package compromise",
                        "summary": "Malicious axios release briefly shipped.",
                        "url": "https://example.com/axios",
                    }
                ],
            }
        ),
    )
    monkeypatch.setattr(
        "bcn.workflows.ai_review.has_ai_review",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr("bcn.workflows.ai_review.run_openai_editorial_review", review_mock)
    insert_mock = AsyncMock(return_value=uuid4())
    monkeypatch.setattr("bcn.workflows.ai_review.insert_ai_review", insert_mock)

    result = await run_briefing_ai_review(
        _make_settings(),
        briefing_id=briefing_id,
        source="auto_distribution",
    )

    assert result["status"] == "stored"
    assert result["decision"] == "edit"
    assert (
        review_mock.await_args.args[1].selected_items_context[0]["title"]
        == "Axios package compromise"
    )
    insert_mock.assert_awaited_once()
    assert insert_mock.await_args.kwargs["briefing_id"] == briefing_id
    assert insert_mock.await_args.kwargs["run_id"] == run_id


@pytest.mark.asyncio
async def test_run_prepublish_ai_review_gate_rewrites_on_edit(monkeypatch):
    monkeypatch.setattr(
        "bcn.workflows.ai_review.run_openai_editorial_review",
        AsyncMock(
            return_value=AIReviewResult(
                reviewer_provider="openai",
                reviewer_model="gpt-5.4-2026-03-05",
                reasoning_effort="high",
                decision="edit",
                issue_tags=["unsupported_claim"],
                notes="Rewrite the opener and tighten claims.",
                edited_markdown="**Edited draft**",
                raw_response={"extracted_review": {"decision": "edit"}},
            )
        ),
    )

    result = await run_prepublish_ai_review_gate(
        _make_settings(),
        content_markdown="**Draft**",
        selected_items=[
            {
                "title": "Traefik middleware escape",
                "summary": "Bad middleware path opens the wrong backend.",
                "url": "https://example.com/traefik",
            }
        ],
        latest_run_model="nemotron",
        rewrite_count=2,
    )

    assert result.action == "rewrite"
    assert result.markdown == "**Edited draft**"
    assert result.as_gate_payload()["passed"] is True


@pytest.mark.asyncio
async def test_run_prepublish_ai_review_gate_passes_length_budget_to_editor(monkeypatch):
    from bcn.workflows.ai_review import AIReviewInput
    from bcn.workflows.ai_review import _build_user_prompt

    review_mock = AsyncMock(
        return_value=AIReviewResult(
            reviewer_provider="openai",
            reviewer_model="gpt-5.6",
            reasoning_effort="xhigh",
            decision="accept",
            issue_tags=[],
            notes=None,
            edited_markdown=None,
            raw_response={},
        )
    )
    monkeypatch.setattr("bcn.workflows.ai_review.run_openai_editorial_review", review_mock)

    result = await run_prepublish_ai_review_gate(
        _make_settings(),
        content_markdown="**Draft**",
        selected_items=[],
        length_budget=(1200, 2400, 3100),
    )

    assert result.action == "approve"
    sent: AIReviewInput = review_mock.await_args.args[1]
    assert sent.length_budget == (1200, 2400, 3100)
    prompt = _build_user_prompt(sent)
    assert "Length budget for any rewrite: 1200-3100 characters (target ~2400)" in prompt
    assert "Length budget" not in _build_user_prompt(
        AIReviewInput(briefing_id=None, content_markdown="**Draft**")
    )


@pytest.mark.asyncio
async def test_run_prepublish_ai_review_gate_applies_needs_work_rewrite(monkeypatch):
    monkeypatch.setattr(
        "bcn.workflows.ai_review.run_openai_editorial_review",
        AsyncMock(
            return_value=AIReviewResult(
                reviewer_provider="openai",
                reviewer_model="gpt-5.4-2026-03-05",
                reasoning_effort="high",
                decision="needs_work",
                issue_tags=["weak_cloud_focus"],
                notes="This draft is too loose for publish.",
                edited_markdown="**Editor rewrite**",
                raw_response={"extracted_review": {"decision": "needs_work"}},
            )
        ),
    )

    result = await run_prepublish_ai_review_gate(
        _make_settings(),
        content_markdown="**Draft**",
        selected_items=[],
        latest_run_model="nemotron",
        rewrite_count=1,
    )

    # A rewrite-bearing needs_work applies the edit (then re-runs release
    # checks upstream) instead of vetoing a draft the editor already fixed.
    assert result.action == "rewrite"
    assert result.markdown == "**Editor rewrite**"


@pytest.mark.asyncio
async def test_run_prepublish_ai_review_gate_blocks_needs_work_without_rewrite(
    monkeypatch,
):
    monkeypatch.setattr(
        "bcn.workflows.ai_review.run_openai_editorial_review",
        AsyncMock(
            return_value=AIReviewResult(
                reviewer_provider="openai",
                reviewer_model="gpt-5.4-2026-03-05",
                reasoning_effort="high",
                decision="needs_work",
                issue_tags=["weak_cloud_focus"],
                notes="This draft is too loose for publish.",
                edited_markdown=None,
                raw_response={"extracted_review": {"decision": "needs_work"}},
            )
        ),
    )

    result = await run_prepublish_ai_review_gate(
        _make_settings(),
        content_markdown="**Draft**",
        selected_items=[],
        latest_run_model="nemotron",
        rewrite_count=1,
    )

    assert result.action == "block"
    assert result.markdown == "**Draft**"
    assert result.as_gate_payload()["passed"] is False
