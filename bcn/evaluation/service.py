"""Service helpers for evaluation lanes and replay orchestration.

These functions own persistence, report writing, and pool lifecycle so the CLI
and scheduler can stay thin and delegate to a single control-plane module.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bcn.common.config import Settings
from bcn.persistence.evaluation import complete_evaluation_run
from bcn.persistence.evaluation import count_simulation_runs
from bcn.persistence.evaluation import create_evaluation_run
from bcn.persistence.evaluation import ensure_evaluation_tables
from bcn.persistence.evaluation import ensure_simulation_tables
from bcn.persistence.evaluation import fail_evaluation_run
from bcn.persistence.evaluation import get_latest_simulation_report
from bcn.persistence.evaluation import insert_simulation_report
from bcn.persistence.runtime import close_pool
from bcn.persistence.runtime import get_pool

from .lanes import build_benchmark_pack
from .lanes import run_benchmark_pack
from .lanes import run_shadow_lane
from .simulation import compare_simulation_reports
from .simulation import simulate_historical_briefings


def _write_json_report(path: str | Path | None, payload: object) -> None:
    """Write JSON to disk with support for UUID/datetime-like objects."""
    if not path:
        return
    target = Path(path)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


async def build_benchmark_pack_artifact(
    settings: Settings,
    *,
    limit: int = 50,
    since_days: int = 90,
    include_unreviewed: bool = False,
    include_nonpublishable: bool = False,
    output_path: str | None = None,
    manage_pool: bool = True,
) -> dict[str, Any]:
    """Build and optionally write a curated benchmark pack."""
    await get_pool(settings)
    try:
        pack = await build_benchmark_pack(
            settings,
            limit=max(0, int(limit)),
            since_days=max(0, int(since_days)),
            include_unreviewed=include_unreviewed,
            include_nonpublishable=include_nonpublishable,
        )
        _write_json_report(output_path, pack)
        return pack
    finally:
        if manage_pool:
            await close_pool()


async def execute_simulation_lane(
    settings: Settings,
    *,
    limit: int = 30,
    since_days: int = 90,
    output_path: str = "simulation_report.json",
    include_text: bool = False,
    with_critic_rewrites: bool = False,
    reanalyze_items: bool = False,
    store_db: bool = True,
    source: str = "cli",
    manage_pool: bool = True,
) -> dict[str, Any]:
    """Run the replay lane, persist if requested, and write the report."""
    await get_pool(settings)
    out_file = Path(output_path)
    try:
        baseline_report: dict[str, Any] | None = None

        if store_db:
            await ensure_simulation_tables()
            existing_runs = await count_simulation_runs()
            if existing_runs == 0 and out_file.exists():
                try:
                    previous_payload = json.loads(out_file.read_text(encoding="utf-8"))
                    if isinstance(previous_payload, dict) and isinstance(
                        previous_payload.get("results"), list
                    ):
                        await insert_simulation_report(
                            previous_payload,
                            report_path=str(out_file),
                            source="imported_file",
                            notes="Imported from existing simulation output file.",
                        )
                except Exception:
                    # Preserve current behavior: ignore unreadable local baseline files.
                    pass
            baseline_report = await get_latest_simulation_report()

        report = await simulate_historical_briefings(
            settings=settings,
            limit=max(0, int(limit)),
            since_days=max(0, int(since_days)),
            include_text=include_text,
            apply_critic_rewrites=with_critic_rewrites,
            reanalyze_items=reanalyze_items,
        )

        if store_db:
            run_id = await insert_simulation_report(
                report,
                report_path=str(out_file),
                source=source,
            )
            report["db_run_id"] = str(run_id)
            if baseline_report:
                comparison = compare_simulation_reports(report, baseline_report)
                comparison["baseline_db_run_id"] = baseline_report.get("db_run_id")
                report["comparison_to_previous_run"] = comparison

        _write_json_report(out_file, report)
        return report
    finally:
        if manage_pool:
            await close_pool()


async def execute_benchmark_lane(
    settings: Settings,
    *,
    cases_path: str,
    candidate_overrides_path: str | None = None,
    output_path: str | None = "benchmark_report.json",
    include_text: bool = False,
    store_db: bool = True,
    source: str = "cli",
    notes: str | None = None,
    manage_pool: bool = True,
) -> dict[str, Any]:
    """Run the benchmark lane and optionally persist/write the report."""
    await get_pool(settings)
    run_id = None
    try:
        if store_db:
            await ensure_evaluation_tables()
            run_id = await create_evaluation_run(
                lane="benchmark",
                source=source,
                report_path=str(output_path) if output_path else None,
                pack_path=cases_path,
                notes=notes,
            )

        report = await run_benchmark_pack(
            settings,
            cases_path=cases_path,
            candidate_overrides_path=candidate_overrides_path,
            include_text=include_text,
        )
        _write_json_report(output_path, report)
        if store_db and run_id is not None:
            await complete_evaluation_run(
                run_id,
                report,
                report_path=str(output_path) if output_path else None,
                notes=notes,
            )
            report["db_run_id"] = str(run_id)
            _write_json_report(output_path, report)
        return report
    except Exception as exc:
        if store_db and run_id is not None:
            await fail_evaluation_run(run_id, error_message=str(exc), notes=notes)
        raise
    finally:
        if manage_pool:
            await close_pool()


async def execute_shadow_lane(
    settings: Settings,
    *,
    workflow_mode: str,
    candidate_overrides_path: str | None = None,
    output_path: str | None = "shadow_report.json",
    include_text: bool = False,
    store_db: bool = True,
    source: str = "cli",
    notes: str | None = None,
    manage_pool: bool = True,
) -> dict[str, Any]:
    """Run the live shadow lane and optionally persist/write the report."""
    await get_pool(settings)
    run_id = None
    try:
        if store_db:
            await ensure_evaluation_tables()
            run_id = await create_evaluation_run(
                lane="shadow",
                source=source,
                report_path=str(output_path) if output_path else None,
                workflow_mode=workflow_mode,
                notes=notes,
            )

        report = await run_shadow_lane(
            settings,
            workflow_mode=workflow_mode,
            candidate_overrides_path=candidate_overrides_path,
            include_text=include_text,
        )
        _write_json_report(output_path, report)
        if store_db and run_id is not None:
            await complete_evaluation_run(
                run_id,
                report,
                report_path=str(output_path) if output_path else None,
                notes=notes,
            )
            report["db_run_id"] = str(run_id)
            _write_json_report(output_path, report)
        return report
    except Exception as exc:
        if store_db and run_id is not None:
            await fail_evaluation_run(run_id, error_message=str(exc), notes=notes)
        raise
    finally:
        if manage_pool:
            await close_pool()
