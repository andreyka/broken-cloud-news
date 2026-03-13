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
from bcn.optimization.runner import execute_optimization_run
from bcn.optimization.scoring import score_optimization_candidate


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


def test_score_optimization_candidate_rejects_quality_regressions():
    champion_replay = {
        "summary": {
            "avg_simulated_score": 88,
            "gate_quality": {"simulated_hard_pass_rate": 1.0},
            "focus_metrics": {
                "human_writer_pass_rate_simulated": 1.0,
                "formatting_clean_pass_rate_simulated": 1.0,
                "duplicate_link_issue_rate_simulated": 0.0,
                "duplicate_story_signal_rate_simulated": 0.0,
            },
        }
    }
    candidate_replay = {
        "summary": {
            "avg_simulated_score": 84,
            "gate_quality": {"simulated_hard_pass_rate": 0.8},
            "focus_metrics": {
                "human_writer_pass_rate_simulated": 0.8,
                "formatting_clean_pass_rate_simulated": 1.0,
                "duplicate_link_issue_rate_simulated": 0.1,
                "duplicate_story_signal_rate_simulated": 0.0,
            },
        }
    }
    benchmark_report = {
        "summary": {
            "candidate_vs_champion_case_pass_delta": -0.1,
        }
    }

    summary = score_optimization_candidate(
        champion_replay=champion_replay,
        candidate_replay=candidate_replay,
        benchmark_report=benchmark_report,
    )

    assert summary["hard_reject"] is True
    assert "benchmark_case_pass_regressed" in summary["hard_reject_reasons"]
    assert summary["recommendation"] == "reject"


def test_score_optimization_candidate_can_promote_candidate():
    champion_replay = {
        "summary": {
            "avg_simulated_score": 84,
            "gate_quality": {"simulated_hard_pass_rate": 0.9},
            "focus_metrics": {
                "human_writer_pass_rate_simulated": 0.7,
                "formatting_clean_pass_rate_simulated": 0.9,
                "duplicate_link_issue_rate_simulated": 0.0,
                "duplicate_story_signal_rate_simulated": 0.0,
            },
        }
    }
    candidate_replay = {
        "summary": {
            "avg_simulated_score": 87,
            "gate_quality": {"simulated_hard_pass_rate": 0.9},
            "focus_metrics": {
                "human_writer_pass_rate_simulated": 0.8,
                "formatting_clean_pass_rate_simulated": 0.9,
                "duplicate_link_issue_rate_simulated": 0.0,
                "duplicate_story_signal_rate_simulated": 0.0,
            },
        }
    }
    benchmark_report = {
        "summary": {
            "candidate_vs_champion_case_pass_delta": 0.1,
        }
    }

    summary = score_optimization_candidate(
        champion_replay=champion_replay,
        candidate_replay=candidate_replay,
        benchmark_report=benchmark_report,
    )

    assert summary["hard_reject"] is False
    assert summary["recommendation"] == "promote_candidate"


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


def test_optimize_run_command_reports_summary(monkeypatch, tmp_path):
    runner = CliRunner()
    variant_path = tmp_path / "variant.json"
    variant_path.write_text(
        json.dumps({"id": "test-variant", "settings_overrides": {}}),
        encoding="utf-8",
    )
    run_mock = AsyncMock(
        return_value={
            "variant": {"id": "test-variant"},
            "output_dir": str(tmp_path / "artifacts"),
            "db_run_id": "run-id",
            "db_candidate_id": "candidate-id",
            "summary": {
                "recommendation": "eligible",
                "composite_score": 3.5,
                "hard_reject_reasons": [],
            },
        }
    )
    monkeypatch.setattr("bcn.optimization.execute_optimization_run", run_mock)

    result = runner.invoke(
        cli_module.cli,
        ["optimize-run", "--variant", str(variant_path)],
    )

    assert result.exit_code == 0
    assert "Optimization variant=test-variant recommendation=eligible score=3.5" in result.output
    assert "DB run id: run-id" in result.output
    assert run_mock.await_count == 1


async def _noop_async_dict(payload):
    return payload


def test_execute_optimization_run_manages_pool(monkeypatch, tmp_path):
    variant_path = tmp_path / "variant.json"
    variant_path.write_text(
        json.dumps({"id": "rewrite-budget-7", "settings_overrides": {}}),
        encoding="utf-8",
    )
    benchmark_path = tmp_path / "cases.json"
    benchmark_path.write_text(json.dumps({"cases": []}), encoding="utf-8")

    get_pool_mock = AsyncMock()
    close_pool_mock = AsyncMock()
    monkeypatch.setattr("bcn.optimization.runner.get_pool", get_pool_mock)
    monkeypatch.setattr("bcn.optimization.runner.close_pool", close_pool_mock)
    monkeypatch.setattr(
        "bcn.optimization.runner.execute_simulation_lane",
        AsyncMock(
            side_effect=[
                {
                    "summary": {
                        "avg_simulated_score": 80,
                        "gate_quality": {"simulated_hard_pass_rate": 1.0},
                        "focus_metrics": {
                            "human_writer_pass_rate_simulated": 1.0,
                            "formatting_clean_pass_rate_simulated": 1.0,
                            "duplicate_link_issue_rate_simulated": 0.0,
                            "duplicate_story_signal_rate_simulated": 0.0,
                        },
                    }
                },
                {
                    "summary": {
                        "avg_simulated_score": 82,
                        "gate_quality": {"simulated_hard_pass_rate": 1.0},
                        "focus_metrics": {
                            "human_writer_pass_rate_simulated": 1.0,
                            "formatting_clean_pass_rate_simulated": 1.0,
                            "duplicate_link_issue_rate_simulated": 0.0,
                            "duplicate_story_signal_rate_simulated": 0.0,
                        },
                    }
                },
            ]
        ),
    )
    monkeypatch.setattr(
        "bcn.optimization.runner.execute_benchmark_lane",
        AsyncMock(
            return_value={
                "summary": {
                    "candidate_case_pass_rate": 1.0,
                    "candidate_vs_champion_case_pass_delta": 0.0,
                }
            }
        ),
    )

    import asyncio

    result = asyncio.run(
        execute_optimization_run(
            Settings(),
            variant_path=str(variant_path),
            benchmark_pack_path=str(benchmark_path),
            replay_limit=2,
            replay_since_days=30,
            benchmark_since_days=30,
            output_dir=str(tmp_path / "artifacts"),
            store_db=False,
        )
    )

    assert result["variant"]["id"] == "rewrite-budget-7"
    assert get_pool_mock.await_count == 1
    assert close_pool_mock.await_count == 1


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
