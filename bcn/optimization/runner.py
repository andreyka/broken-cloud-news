"""Runner for offline prompt/policy optimization experiments."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
from typing import Any

from bcn.common.config import Settings
from bcn.evaluation.service import build_benchmark_pack_artifact
from bcn.evaluation.service import execute_benchmark_lane
from bcn.evaluation.service import execute_simulation_lane
from bcn.optimization.scoring import score_optimization_candidate
from bcn.optimization.variants import load_variant_spec
from bcn.persistence.optimization import complete_optimization_candidate
from bcn.persistence.optimization import complete_optimization_run
from bcn.persistence.optimization import create_optimization_run
from bcn.persistence.optimization import fail_optimization_candidate
from bcn.persistence.optimization import fail_optimization_run
from bcn.persistence.optimization import insert_optimization_candidate
from bcn.persistence.optimization import (
    insert_optimization_candidate_lane_result,
)


def _git_sha() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return ""
    return completed.stdout.strip()


async def execute_optimization_run(
    settings: Settings,
    *,
    variant_path: str,
    benchmark_pack_path: str | None = None,
    replay_limit: int = 20,
    replay_since_days: int = 60,
    benchmark_since_days: int = 90,
    output_dir: str = "optimization_runs",
    store_db: bool = True,
    source: str = "cli",
) -> dict[str, Any]:
    """Run champion vs candidate replay+benchmark for one variant spec."""
    variant_spec, overrides = load_variant_spec(variant_path)
    variant_id = str(variant_spec["id"])
    out_dir = Path(output_dir).resolve() / variant_id
    out_dir.mkdir(parents=True, exist_ok=True)

    run_id = None
    candidate_id = None
    try:
        if not benchmark_pack_path:
            benchmark_pack_path = str(out_dir / "benchmark_pack.json")
            await build_benchmark_pack_artifact(
                settings,
                limit=50,
                since_days=max(0, int(benchmark_since_days)),
                include_unreviewed=True,
                include_nonpublishable=True,
                output_path=benchmark_pack_path,
            )

        if store_db:
            run_id = await create_optimization_run(
                source=source,
                git_sha=_git_sha(),
                benchmark_pack_path=benchmark_pack_path,
                replay_limit=max(0, int(replay_limit)),
                replay_since_days=max(0, int(replay_since_days)),
                notes=str(variant_spec.get("description") or "").strip() or None,
            )
            candidate_id = await insert_optimization_candidate(
                optimization_run_id=run_id,
                variant_id=variant_id,
                base_variant=str(variant_spec.get("base") or "champion"),
                variant_payload=variant_spec,
            )

        champion_replay = await execute_simulation_lane(
            settings,
            limit=max(0, int(replay_limit)),
            since_days=max(0, int(replay_since_days)),
            output_path=str(out_dir / "replay_champion.json"),
            include_text=True,
            with_critic_rewrites=False,
            reanalyze_items=False,
            store_db=False,
            manage_pool=True,
        )
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".json",
            delete=False,
        ) as handle:
            json.dump(overrides, handle, ensure_ascii=False, indent=2)
            overrides_path = handle.name

        candidate_replay = await execute_simulation_lane(
            settings,
            limit=max(0, int(replay_limit)),
            since_days=max(0, int(replay_since_days)),
            candidate_overrides_path=overrides_path,
            output_path=str(out_dir / "replay_candidate.json"),
            include_text=True,
            with_critic_rewrites=False,
            reanalyze_items=False,
            store_db=False,
            manage_pool=True,
        )
        benchmark_report = await execute_benchmark_lane(
            settings,
            cases_path=str(benchmark_pack_path),
            candidate_overrides_path=overrides_path,
            output_path=str(out_dir / "benchmark.json"),
            include_text=True,
            store_db=False,
            manage_pool=True,
        )

        summary = score_optimization_candidate(
            champion_replay=champion_replay,
            candidate_replay=candidate_replay,
            benchmark_report=benchmark_report,
        )

        if store_db and candidate_id is not None:
            await insert_optimization_candidate_lane_result(
                optimization_candidate_id=candidate_id,
                lane="replay_champion",
                report=champion_replay,
                summary=champion_replay.get("summary", {}),
                hard_reject=False,
                score=float(
                    champion_replay.get("summary", {}).get("avg_simulated_score", 0.0)
                ),
            )
            await insert_optimization_candidate_lane_result(
                optimization_candidate_id=candidate_id,
                lane="replay_candidate",
                report=candidate_replay,
                summary=candidate_replay.get("summary", {}),
                hard_reject=bool(summary.get("hard_reject")),
                score=float(
                    candidate_replay.get("summary", {}).get("avg_simulated_score", 0.0)
                ),
            )
            await insert_optimization_candidate_lane_result(
                optimization_candidate_id=candidate_id,
                lane="benchmark",
                report=benchmark_report,
                summary=benchmark_report.get("summary", {}),
                hard_reject=bool(summary.get("hard_reject")),
                score=float(
                    benchmark_report.get("summary", {}).get(
                        "candidate_case_pass_rate",
                        0.0,
                    )
                ),
            )
            await complete_optimization_candidate(
                candidate_id,
                hard_reject=bool(summary["hard_reject"]),
                recommendation=str(summary["recommendation"]),
                composite_score=float(summary["composite_score"]),
                summary=summary,
            )
            if run_id is not None:
                await complete_optimization_run(run_id)

        report = {
            "variant": variant_spec,
            "output_dir": str(out_dir),
            "benchmark_pack_path": str(benchmark_pack_path),
            "champion_replay": champion_replay.get("summary", {}),
            "candidate_replay": candidate_replay.get("summary", {}),
            "benchmark": benchmark_report.get("summary", {}),
            "summary": summary,
            "db_run_id": str(run_id) if run_id else None,
            "db_candidate_id": str(candidate_id) if candidate_id else None,
        }
        (out_dir / "optimization_summary.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return report
    except Exception as exc:
        if store_db and candidate_id is not None:
            await fail_optimization_candidate(
                candidate_id,
                summary={"error": str(exc), "variant": variant_spec},
            )
        if store_db and run_id is not None:
            await fail_optimization_run(run_id, notes=str(exc))
        raise
