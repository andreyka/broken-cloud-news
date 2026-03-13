from __future__ import annotations

from datetime import datetime
from datetime import timezone
import json
from unittest.mock import AsyncMock

from click.testing import CliRunner

import bcn.cli as cli_module


def test_export_training_includes_shadow_preference_rows(monkeypatch, tmp_path):
    runner = CliRunner()
    created_at = datetime(2026, 3, 7, 8, 15, tzinfo=timezone.utc)
    shadow_report = {
        "workflow_mode": "regular_daily_briefing",
        "selection_overlap_ratio": 0.8,
        "summary": {
            "recommendation": "promote_candidate",
            "confidence": "high",
            "score_delta": 5,
        },
        "champion": {
            "markdown": "champion draft",
            "selected_items": [{"id": "item-1", "title": "Alpha"}],
        },
        "candidate": {
            "markdown": "candidate draft",
            "selected_items": [{"id": "item-1", "title": "Alpha"}],
        },
    }

    monkeypatch.setattr("bcn.persistence.runtime.get_pool", AsyncMock())
    monkeypatch.setattr("bcn.persistence.runtime.close_pool", AsyncMock())
    monkeypatch.setattr(
        "bcn.persistence.optimization.get_optimization_candidates_for_export",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "bcn.persistence.optimization.get_optimization_candidate_lane_results",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "bcn.persistence.training.get_generation_runs_for_export",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "bcn.persistence.training.get_generation_rounds_for_runs",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "bcn.persistence.training.get_generation_preference_pairs_for_runs",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "bcn.persistence.training.get_human_reviews",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "bcn.persistence.training.get_distribution_outcomes",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "bcn.persistence.evaluation.get_evaluation_runs_for_export",
        AsyncMock(
            return_value=[
                {
                    "id": "shadow-run-1",
                    "created_at": created_at,
                    "generated_at": created_at,
                    "workflow_mode": "regular_daily_briefing",
                    "candidate_overrides": {
                        "llm_model_writer": "Qwen/Qwen3-VL-30B-A3B-Instruct-FP8"
                    },
                    "summary": shadow_report["summary"],
                    "report": shadow_report,
                }
            ]
        ),
    )

    result = runner.invoke(
        cli_module.cli,
        ["export-training", "--output-dir", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert "shadow_preference_rows=1" in result.output

    pref_path = tmp_path / "preference.jsonl"
    shadow_trace_path = tmp_path / "shadow_trace.jsonl"
    manifest_path = tmp_path / "manifest.json"
    assert pref_path.exists()
    assert shadow_trace_path.exists()
    assert manifest_path.exists()

    pref_rows = [
        json.loads(line)
        for line in pref_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(pref_rows) == 1
    assert pref_rows[0]["source"] == "shadow_lane"
    assert pref_rows[0]["chosen"] == "candidate draft"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["shadow_preference_rows"] == 1
    assert manifest["shadow_trace_rows"] == 1


def test_export_training_includes_optimization_preference_rows(monkeypatch, tmp_path):
    runner = CliRunner()
    created_at = datetime(2026, 3, 12, 8, 15, tzinfo=timezone.utc)
    benchmark_report = {
        "results": [
            {
                "case_id": "case-1",
                "expected_decision": "accept",
                "issue_tags": ["overclaim"],
                "selected_items": [{"id": "item-1", "title": "Alpha"}],
                "history": [{"id": "briefing-1", "content_markdown": "old"}],
                "champion": {
                    "markdown": "champion benchmark draft",
                    "case_pass": False,
                    "rubric": {"score": 80},
                },
                "candidate": {
                    "markdown": "candidate benchmark draft",
                    "case_pass": True,
                    "rubric": {"score": 86},
                },
            }
        ]
    }
    replay_champion = {
        "results": [
            {
                "briefing_id": "briefing-1",
                "simulated_markdown": "champion replay draft",
                "simulated_score": 79,
            }
        ]
    }
    replay_candidate = {
        "results": [
            {
                "briefing_id": "briefing-1",
                "simulated_markdown": "candidate replay draft",
                "simulated_score": 84,
                "selected_items": [{"id": "item-1", "title": "Alpha"}],
                "simulated_selected_items": [{"id": "item-1", "title": "Alpha"}],
                "history": [{"id": "briefing-0", "content_markdown": "prior"}],
            }
        ]
    }

    monkeypatch.setattr("bcn.persistence.runtime.get_pool", AsyncMock())
    monkeypatch.setattr("bcn.persistence.runtime.close_pool", AsyncMock())
    monkeypatch.setattr(
        "bcn.persistence.training.get_generation_runs_for_export",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "bcn.persistence.training.get_generation_rounds_for_runs",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "bcn.persistence.training.get_generation_preference_pairs_for_runs",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "bcn.persistence.training.get_human_reviews",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "bcn.persistence.training.get_distribution_outcomes",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "bcn.persistence.evaluation.get_evaluation_runs_for_export",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "bcn.persistence.optimization.get_optimization_candidates_for_export",
        AsyncMock(
            return_value=[
                {
                    "id": "candidate-1",
                    "optimization_run_id": "run-1",
                    "variant_id": "rewrite-budget-7",
                    "base_variant": "champion",
                    "variant_payload": {"id": "rewrite-budget-7"},
                    "summary": {"recommendation": "promote_candidate"},
                    "created_at": created_at,
                    "git_sha": "abc123",
                    "benchmark_pack_path": "/tmp/pack.json",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        "bcn.persistence.optimization.get_optimization_candidate_lane_results",
        AsyncMock(
            return_value=[
                {
                    "optimization_candidate_id": "candidate-1",
                    "lane": "benchmark",
                    "report": benchmark_report,
                },
                {
                    "optimization_candidate_id": "candidate-1",
                    "lane": "replay_champion",
                    "report": replay_champion,
                },
                {
                    "optimization_candidate_id": "candidate-1",
                    "lane": "replay_candidate",
                    "report": replay_candidate,
                },
            ]
        ),
    )

    result = runner.invoke(
        cli_module.cli,
        ["export-training", "--output-dir", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert "optimization_preference_rows=2" in result.output

    pref_rows = [
        json.loads(line)
        for line in (tmp_path / "preference.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(pref_rows) == 2
    sources = {row["source"] for row in pref_rows}
    assert sources == {"optimization_benchmark", "optimization_replay"}

    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["optimization_preference_rows"] == 2
    assert manifest["optimization_trace_rows"] == 1
