from __future__ import annotations

from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch
from uuid import uuid4

import pytest

from bcn.services.writer.service import WriterService
from bcn.common.config import Settings
from bcn.contracts.services import WriterTraceMetadata
from bcn.workflows.generation import execute_generation
from bcn.workflows.generation import execute_generation_result
from bcn.workflows.modes.common import parse_writer_handoff_payload
from bcn.workflows.modes.common import WriterHandoffResult


def _make_settings(**overrides) -> Settings:
    defaults = {
        "database_url": "postgresql://test:test@localhost:5432/test",
        "llm_base_url": "http://fake-llm:8000/v1",
        "llm_model": "test-model",
        "comfyui_url": "http://fake-comfy:8188",
        "briefing_skip_if_no_high_signal": True,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _make_writer_service(
    *,
    selected_items: list[dict],
    candidate: dict,
    artifact: dict | None = None,
    selection_plan: dict | None = None,
):
    service = SimpleNamespace()
    service.settings = _make_settings()
    service.selector = SimpleNamespace(
        high_signal_count=lambda items: 1,
        is_actionable=lambda item: True,
        passes_source_floor=lambda item: True,
        is_duplicate_of=lambda item, others: False,
    )
    service.is_quiet_day = lambda items: False
    service.select_items_for_briefing = (
        lambda items, recent_published=None, quiet_mode=False: selected_items
    )
    service.select_items_for_monthly_newsletter = lambda items: selected_items
    service.select_items_for_workflow = AsyncMock(
        return_value=selection_plan
        or {
            "decision": "generate",
            "reason": "selection_ready",
            "message": "",
            "mode": "standard",
            "selected_items": selected_items,
        }
    )
    service.generate_release_candidate = AsyncMock(return_value=candidate)
    service.build_release_artifact = AsyncMock(return_value=artifact or {})
    service.get_trace_metadata = AsyncMock(
        return_value=WriterTraceMetadata(
            llm_model="writer:model@v1",
            llm_model_version="v1",
            prompts={"writer": "v1"},
        )
    )
    service.close = AsyncMock()
    return service


def test_build_preference_rationale_summarizes_feedback():
    rationale = WriterService.build_preference_rationale(
        [
            "Fix links.",
            "Tighten sourcing",
            "Clarify operator action",
            "Remove repetition",
        ]
    )

    assert rationale == (
        "Fix links; Tighten sourcing; Clarify operator action; additional release feedback"
    )


@pytest.mark.asyncio
async def test_execute_generation_publishes_and_persists_trace():
    settings = _make_settings()
    item_id = uuid4()
    dropped_item_id = uuid4()
    briefing_id = uuid4()
    run_id = uuid4()
    selected_items = [
        {
            "id": item_id,
            "title": "Kubernetes issue",
            "summary": "Critical cluster issue",
            "url": "https://example.com/item",
        },
        {
            "id": dropped_item_id,
            "title": "Secondary issue",
            "summary": "Lower-priority issue",
            "url": "https://example.com/dropped",
        }
    ]
    final_selected_items = [selected_items[0]]
    candidate = {
        "markdown": "Final body",
        "gate": {"passed": True},
        "critique": {"passed": True, "score": 95, "issues": [], "recommendations": []},
        "verifier": {"passed": True, "issues": [], "recommendations": []},
        "release_passed": True,
        "rewrites": 1,
        "selected_items": final_selected_items,
        "rounds": [
            {
                "round_index": 0,
                "phase": "initial",
                "draft_input": "Initial body",
                "gate_result": {"passed": False},
                "critique_result": {"passed": False},
                "verifier_result": {"passed": True},
                "feedback": ["Fix links"],
                "rewrite_output": "Final body",
                "passed": False,
            },
            {
                "round_index": 1,
                "phase": "rewrite",
                "draft_input": "Final body",
                "gate_result": {"passed": True},
                "critique_result": {"passed": True},
                "verifier_result": {"passed": True},
                "feedback": [],
                "rewrite_output": None,
                "passed": True,
            },
        ],
        "preference_pairs": [
            {
                "round_index": 1,
                "chosen_text": "Final body",
                "rejected_text": "Initial body",
                "rationale": "Fix links",
                "source": "auto_writer_loop",
            }
        ],
    }
    service = _make_writer_service(
        selected_items=selected_items,
        candidate=candidate,
        artifact={
            "cover_prompt": "cover prompt",
            "cover_url": "https://example.com/cover.png",
            "markdown": "# Final",
            "html": "<h1>Final</h1>",
        },
    )

    with (
        patch("bcn.workflows.generation.get_pool", new_callable=AsyncMock),
        patch(
            "bcn.workflows.generation.finalize_stale_pending_generation_runs",
            new_callable=AsyncMock,
            return_value=0,
        ),
        patch(
            "bcn.workflows.generation.get_analyzed_items",
            new_callable=AsyncMock,
            return_value=[
                {
                    "id": item_id,
                    "status": "WRITING",
                    "title": "Kubernetes issue",
                    "summary": "Critical cluster issue",
                    "url": "https://example.com/item",
                    "published_at": datetime.now(timezone.utc),
                },
                {
                    "id": dropped_item_id,
                    "status": "WRITING",
                    "title": "Secondary issue",
                    "summary": "Lower-priority issue",
                    "url": "https://example.com/dropped",
                    "published_at": datetime.now(timezone.utc),
                }
            ],
        ),
        patch(
            "bcn.workflows.generation.preview_generation_candidate_pool",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "bcn.workflows.generation.get_recent_briefings",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "bcn.workflows.generation.get_recent_published_items",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_recent_published,
        patch(
            "bcn.workflows.generation.create_generation_run",
            new_callable=AsyncMock,
            return_value=run_id,
        ) as mock_create_run,
        patch(
            "bcn.workflows.generation.append_generation_round",
            new_callable=AsyncMock,
        ) as mock_append_round,
        patch(
            "bcn.workflows.generation.insert_generation_preference_pair",
            new_callable=AsyncMock,
        ) as mock_insert_pair,
        patch(
            "bcn.workflows.generation.insert_briefing",
            new_callable=AsyncMock,
            return_value=briefing_id,
        ) as mock_insert_briefing,
        patch(
            "bcn.workflows.generation.finalize_generation_run",
            new_callable=AsyncMock,
        ) as mock_finalize,
        patch(
            "bcn.workflows.generation.release_items_from_writing",
            new_callable=AsyncMock,
        ) as mock_release,
    ):
        result = await execute_generation(
            settings,
            mode="regular_daily_briefing",
            writer_service=service,
            manage_pool=False,
        )

    handoff = parse_writer_handoff_payload(result)
    assert handoff is not None
    assert handoff.decision == "publish"
    assert handoff.briefing_id == briefing_id
    assert handoff.item_count == 1
    service.select_items_for_workflow.assert_awaited_once()
    assert service.select_items_for_workflow.await_args.kwargs["recent_published"] == []
    mock_recent_published.assert_awaited_once()
    service.generate_release_candidate.assert_awaited_once()
    service.build_release_artifact.assert_awaited_once_with(
        briefing_body="Final body",
        selected_items=final_selected_items,
        mode="standard",
    )
    mock_create_run.assert_awaited_once()
    assert mock_create_run.await_args.kwargs["selected_item_ids"] == [item_id]
    assert mock_create_run.await_args.kwargs["selected_items"] == final_selected_items
    assert mock_append_round.await_count == 2
    mock_insert_pair.assert_awaited_once()
    mock_insert_briefing.assert_awaited_once()
    assert mock_insert_briefing.await_args.kwargs["item_ids"] == [item_id]
    mock_finalize.assert_awaited_once()
    assert mock_finalize.await_args.kwargs["decision"] == "PUBLISHED"
    mock_release.assert_awaited_once_with([item_id, dropped_item_id])


@pytest.mark.asyncio
async def test_execute_generation_blocks_publish_and_releases_claimed_items():
    settings = _make_settings()
    item_id = uuid4()
    run_id = uuid4()
    selected_items = [
        {
            "id": item_id,
            "title": "Kubernetes issue",
            "summary": "Critical cluster issue",
            "url": "https://example.com/item",
        }
    ]
    candidate = {
        "markdown": "Blocked body",
        "gate": {"passed": False},
        "critique": {"passed": False, "score": 40, "issues": [], "recommendations": []},
        "verifier": {"passed": True, "issues": [], "recommendations": []},
        "release_passed": False,
        "rewrites": 2,
        "rounds": [],
        "preference_pairs": [],
    }
    service = _make_writer_service(selected_items=selected_items, candidate=candidate)

    with (
        patch("bcn.workflows.generation.get_pool", new_callable=AsyncMock),
        patch(
            "bcn.workflows.generation.finalize_stale_pending_generation_runs",
            new_callable=AsyncMock,
            return_value=0,
        ),
        patch(
            "bcn.workflows.generation.get_analyzed_items",
            new_callable=AsyncMock,
            return_value=[
                {
                    "id": item_id,
                    "status": "WRITING",
                    "title": "Kubernetes issue",
                    "summary": "Critical cluster issue",
                    "url": "https://example.com/item",
                    "published_at": datetime.now(timezone.utc),
                }
            ],
        ),
        patch(
            "bcn.workflows.generation.preview_generation_candidate_pool",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "bcn.workflows.generation.get_recent_briefings",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "bcn.workflows.generation.get_recent_published_items",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "bcn.workflows.generation.create_generation_run",
            new_callable=AsyncMock,
            return_value=run_id,
        ),
        patch(
            "bcn.workflows.generation.append_generation_round",
            new_callable=AsyncMock,
        ),
        patch(
            "bcn.workflows.generation.insert_generation_preference_pair",
            new_callable=AsyncMock,
        ),
        patch(
            "bcn.workflows.generation.insert_briefing",
            new_callable=AsyncMock,
        ) as mock_insert_briefing,
        patch(
            "bcn.workflows.generation.finalize_generation_run",
            new_callable=AsyncMock,
        ) as mock_finalize,
        patch(
            "bcn.workflows.generation.release_items_from_writing",
            new_callable=AsyncMock,
        ) as mock_release,
    ):
        result = await execute_generation(
            settings,
            mode="regular_daily_briefing",
            writer_service=service,
            manage_pool=False,
        )

    handoff = parse_writer_handoff_payload(result)
    assert handoff is not None
    assert handoff.decision == "blocked"
    mock_insert_briefing.assert_not_awaited()
    mock_finalize.assert_awaited_once()
    assert mock_finalize.await_args.kwargs["decision"] == "BLOCKED"
    mock_release.assert_awaited_once_with([item_id])


@pytest.mark.asyncio
async def test_execute_generation_finalizes_trace_when_publish_persist_fails():
    settings = _make_settings()
    item_id = uuid4()
    run_id = uuid4()
    selected_items = [
        {
            "id": item_id,
            "title": "Kubernetes issue",
            "summary": "Critical cluster issue",
            "url": "https://example.com/item",
        }
    ]
    candidate = {
        "markdown": "Final body",
        "gate": {"passed": True},
        "critique": {"passed": True, "score": 95, "issues": [], "recommendations": []},
        "verifier": {"passed": True, "issues": [], "recommendations": []},
        "release_passed": True,
        "rewrites": 1,
        "rounds": [],
        "preference_pairs": [],
    }
    service = _make_writer_service(
        selected_items=selected_items,
        candidate=candidate,
        artifact={
            "cover_prompt": "cover prompt",
            "cover_url": "https://example.com/cover.png",
            "markdown": "# Final",
            "html": "<h1>Final</h1>",
        },
    )

    with (
        patch("bcn.workflows.generation.get_pool", new_callable=AsyncMock),
        patch(
            "bcn.workflows.generation.finalize_stale_pending_generation_runs",
            new_callable=AsyncMock,
            return_value=0,
        ),
        patch(
            "bcn.workflows.generation.get_analyzed_items",
            new_callable=AsyncMock,
            return_value=[
                {
                    "id": item_id,
                    "status": "WRITING",
                    "title": "Kubernetes issue",
                    "summary": "Critical cluster issue",
                    "url": "https://example.com/item",
                    "published_at": datetime.now(timezone.utc),
                }
            ],
        ),
        patch(
            "bcn.workflows.generation.preview_generation_candidate_pool",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "bcn.workflows.generation.get_recent_briefings",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "bcn.workflows.generation.get_recent_published_items",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "bcn.workflows.generation.create_generation_run",
            new_callable=AsyncMock,
            return_value=run_id,
        ),
        patch(
            "bcn.workflows.generation.append_generation_round",
            new_callable=AsyncMock,
        ),
        patch(
            "bcn.workflows.generation.insert_generation_preference_pair",
            new_callable=AsyncMock,
        ),
        patch(
            "bcn.workflows.generation.insert_briefing",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db down"),
        ),
        patch(
            "bcn.workflows.generation.finalize_generation_run",
            new_callable=AsyncMock,
        ) as mock_finalize,
        patch(
            "bcn.workflows.generation.release_items_from_writing",
            new_callable=AsyncMock,
        ) as mock_release,
    ):
        result = await execute_generation(
            settings,
            mode="regular_daily_briefing",
            writer_service=service,
            manage_pool=False,
        )

    handoff = parse_writer_handoff_payload(result)
    assert handoff is not None
    assert handoff.decision == "blocked"
    assert "internal writer error" in result.lower()
    assert mock_finalize.await_count == 1
    assert mock_finalize.await_args.kwargs["run_id"] == run_id
    assert mock_finalize.await_args.kwargs["decision"] == "BLOCKED"
    mock_release.assert_awaited_once_with([item_id])


@pytest.mark.asyncio
async def test_execute_generation_result_returns_typed_handoff():
    settings = _make_settings()
    run_id = uuid4()
    service = _make_writer_service(
        selected_items=[],
        candidate={},
        selection_plan={
            "decision": "skip",
            "reason": "no_items_remained_after_selection_constraints",
            "message": "No items remained after selection constraints. Skipping briefing.",
            "mode": "quiet_day",
            "selected_items": [],
        },
    )

    with (
        patch("bcn.workflows.generation.get_pool", new_callable=AsyncMock),
        patch(
            "bcn.workflows.generation.finalize_stale_pending_generation_runs",
            new_callable=AsyncMock,
            return_value=0,
        ),
        patch(
            "bcn.workflows.generation.get_analyzed_items",
            new_callable=AsyncMock,
            return_value=[
                {
                    "id": uuid4(),
                    "status": "WRITING",
                    "title": "Low-signal issue",
                    "summary": "Skipped issue",
                    "url": "https://example.com/skipped",
                    "published_at": datetime.now(timezone.utc),
                }
            ],
        ),
        patch(
            "bcn.workflows.generation.preview_generation_candidate_pool",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "bcn.workflows.generation.release_items_from_writing",
            new_callable=AsyncMock,
        ) as mock_release,
        patch(
            "bcn.workflows.generation.get_recent_published_items",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "bcn.workflows.generation.create_generation_run",
            new_callable=AsyncMock,
            return_value=run_id,
        ) as mock_create_run,
        patch(
            "bcn.workflows.generation.finalize_generation_run",
            new_callable=AsyncMock,
        ) as mock_finalize,
    ):
        result = await execute_generation_result(
            settings,
            mode="regular_daily_briefing",
            writer_service=service,
            manage_pool=False,
        )

    assert isinstance(result, WriterHandoffResult)
    assert result.handoff.decision == "skip"
    assert result.handoff.mode == "regular_daily_briefing"
    assert result.human_message == "No items remained after selection constraints. Skipping briefing."
    service.generate_release_candidate.assert_not_awaited()
    assert mock_create_run.await_count == 1
    selection_trace = mock_create_run.await_args.kwargs["selection_trace"]
    assert selection_trace["decision"] == "skip"
    assert selection_trace["reason"] == "no_items_remained_after_selection_constraints"
    assert selection_trace["writer_input_count"] == 1
    assert selection_trace["selected_count"] == 0
    assert mock_finalize.await_count == 1
    assert mock_finalize.await_args.kwargs["run_id"] == run_id
    assert mock_finalize.await_args.kwargs["decision"] == "BLOCKED"
    mock_release.assert_awaited_once()
