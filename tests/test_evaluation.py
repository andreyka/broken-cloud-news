from __future__ import annotations

from datetime import datetime
from datetime import timezone
import json
from unittest.mock import AsyncMock
from uuid import uuid4

from click.testing import CliRunner

import bcn.cli as cli_module
from bcn.common.config import Settings
from bcn.evaluation import build_benchmark_summary
from bcn.evaluation import build_shadow_preference_pair
from bcn.evaluation import build_shadow_summary
from bcn.evaluation import load_settings_with_overrides


def _benchmark_row(
    *,
    champion_case_pass: bool,
    candidate_case_pass: bool,
    champion_score: int,
    candidate_score: int,
    reference_score: int = 80,
    champion_release: bool = True,
    candidate_release: bool = True,
) -> dict[str, object]:
    return {
        "champion": {
            "case_pass": champion_case_pass,
            "release_passed": champion_release,
            "rubric": {"score": champion_score},
        },
        "candidate": {
            "case_pass": candidate_case_pass,
            "release_passed": candidate_release,
            "rubric": {"score": candidate_score},
        },
        "reference": {"rubric": {"score": reference_score}},
    }


def test_build_benchmark_summary_can_promote_candidate():
    rows = [
        _benchmark_row(
            champion_case_pass=True,
            candidate_case_pass=True,
            champion_score=82,
            candidate_score=87,
        ),
        _benchmark_row(
            champion_case_pass=False,
            candidate_case_pass=True,
            champion_score=76,
            candidate_score=84,
        ),
        _benchmark_row(
            champion_case_pass=True,
            candidate_case_pass=True,
            champion_score=81,
            candidate_score=86,
        ),
    ]

    summary = build_benchmark_summary(rows)

    assert summary["recommendation"] == "promote_candidate"
    assert summary["candidate_case_pass_rate"] > summary["champion_case_pass_rate"]


def test_build_shadow_summary_prefers_candidate_when_only_candidate_passes():
    report = {
        "selection_overlap_ratio": 0.5,
        "champion": {
            "decision": "generate",
            "release_passed": False,
            "rubric": {"score": 74},
            "gate": {"hard_issues": ["Missing selected URL"]},
            "verifier": {"score": 40},
        },
        "candidate": {
            "decision": "generate",
            "release_passed": True,
            "rubric": {"score": 82},
            "gate": {"hard_issues": []},
            "verifier": {"score": 78},
        },
    }

    summary = build_shadow_summary(report)

    assert summary["recommendation"] == "promote_candidate"
    assert summary["candidate_release_passed"] is True


def test_build_shadow_preference_pair_prefers_candidate_on_high_confidence_report():
    report = {
        "workflow_mode": "regular_daily_briefing",
        "selection_overlap_ratio": 0.75,
        "summary": {
            "recommendation": "promote_candidate",
            "confidence": "high",
            "score_delta": 6,
        },
        "champion": {
            "markdown": "champion draft",
            "selected_items": [{"id": "a", "title": "Alpha"}],
        },
        "candidate": {
            "markdown": "candidate draft",
            "selected_items": [{"id": "a", "title": "Alpha"}],
        },
    }

    pair = build_shadow_preference_pair(report)

    assert pair is not None
    assert pair["preferred_side"] == "candidate"
    assert pair["chosen"] == "candidate draft"
    assert pair["rejected"] == "champion draft"


def test_build_shadow_preference_pair_skips_low_confidence_or_low_overlap():
    report = {
        "selection_overlap_ratio": 0.4,
        "summary": {
            "recommendation": "promote_candidate",
            "confidence": "low",
        },
        "champion": {"markdown": "champion"},
        "candidate": {"markdown": "candidate"},
    }

    assert build_shadow_preference_pair(report) is None


def test_load_settings_with_overrides_validates_file(tmp_path):
    overrides = tmp_path / "candidate.json"
    overrides.write_text(
        json.dumps({"briefing_critic_min_score": 85, "briefing_history_items": 7}),
        encoding="utf-8",
    )

    settings, payload = load_settings_with_overrides(Settings(), str(overrides))

    assert settings.briefing_critic_min_score == 85
    assert settings.briefing_history_items == 7
    assert payload["briefing_critic_min_score"] == 85


def test_benchmark_pack_command_writes_output(monkeypatch, tmp_path):
    runner = CliRunner()
    output_path = tmp_path / "benchmark_pack.json"
    build_mock = AsyncMock(return_value={"count": 2, "cases": [{"case_id": "x"}]})
    monkeypatch.setattr("bcn.evaluation.service.build_benchmark_pack_artifact", build_mock)

    result = runner.invoke(
        cli_module.cli,
        ["benchmark-pack", "--output", str(output_path)],
    )

    assert result.exit_code == 0
    assert "Benchmark pack written" in result.output
    assert build_mock.await_count == 1


def test_benchmark_command_reports_recommendation(monkeypatch, tmp_path):
    runner = CliRunner()
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps({"cases": []}), encoding="utf-8")
    output_path = tmp_path / "benchmark_report.json"
    run_mock = AsyncMock(
        return_value={
            "count": 4,
            "lane": "benchmark",
            "db_run_id": "benchmark-run-id",
            "summary": {
                "champion_case_pass_rate": 0.5,
                "candidate_case_pass_rate": 0.75,
                "recommendation": "promote_candidate",
                "confidence": "medium",
            },
            "results": [],
        }
    )
    monkeypatch.setattr("bcn.evaluation.service.execute_benchmark_lane", run_mock)

    result = runner.invoke(
        cli_module.cli,
        ["benchmark", "--cases", str(cases_path), "--output", str(output_path)],
    )

    assert result.exit_code == 0
    assert "Benchmark complete" in result.output
    assert "promote_candidate" in result.output
    assert "DB run id: benchmark-run-id" in result.output
    assert run_mock.await_count == 1


def test_shadow_command_reports_recommendation(monkeypatch, tmp_path):
    runner = CliRunner()
    output_path = tmp_path / "shadow_report.json"
    run_mock = AsyncMock(
        return_value={
            "item_pool_count": 12,
            "lane": "shadow",
            "workflow_mode": "regular_daily_briefing",
            "db_run_id": "shadow-run-id",
            "summary": {
                "selection_overlap_ratio": 0.6,
                "recommendation": "hold",
                "confidence": "low",
            },
        }
    )
    monkeypatch.setattr("bcn.evaluation.service.execute_shadow_lane", run_mock)

    result = runner.invoke(
        cli_module.cli,
        ["shadow", "--output", str(output_path)],
    )

    assert result.exit_code == 0
    assert "Shadow evaluation complete" in result.output
    assert "hold" in result.output
    assert "DB run id: shadow-run-id" in result.output
    assert run_mock.await_count == 1


def test_shadow_command_serializes_uuid_report_payload(monkeypatch, tmp_path):
    runner = CliRunner()
    output_path = tmp_path / "shadow_report.json"
    execute_mock = AsyncMock(
        return_value={
            "item_pool_count": 3,
            "lane": "shadow",
            "workflow_mode": "regular_daily_briefing",
            "summary": {
                "selection_overlap_ratio": 0.5,
                "recommendation": "hold",
                "confidence": "low",
            },
            "champion": {
                "selected_items": [
                    {
                        "id": str(uuid4()),
                        "title": "Alpha",
                    }
                ]
            },
            "candidate": {
                "selected_items": [
                    {
                        "id": str(uuid4()),
                        "title": "Beta",
                    }
                ]
            },
        }
    )
    monkeypatch.setattr("bcn.evaluation.service.execute_shadow_lane", execute_mock)

    result = runner.invoke(
        cli_module.cli,
        ["shadow", "--output", str(output_path), "--no-store-db"],
    )

    assert result.exit_code == 0
    assert execute_mock.await_count == 1


def test_evaluation_runs_command_lists_recent_rows(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr("bcn.persistence.runtime.get_pool", AsyncMock())
    monkeypatch.setattr("bcn.persistence.runtime.close_pool", AsyncMock())
    created_at = datetime(2026, 3, 6, 12, 0, tzinfo=timezone.utc)
    list_mock = AsyncMock(
        return_value=[
            {
                "id": "run-1",
                "created_at": created_at,
                "lane": "shadow",
                "count": 1,
                "summary": {"recommendation": "hold", "confidence": "low"},
            }
        ]
    )
    monkeypatch.setattr("bcn.persistence.evaluation.list_recent_evaluation_runs", list_mock)

    result = runner.invoke(cli_module.cli, ["evaluation-runs", "--limit", "5"])

    assert result.exit_code == 0
    assert "lane=shadow" in result.output
    assert "status=completed" in result.output
    assert "run-1" in result.output
    assert list_mock.await_count == 1
