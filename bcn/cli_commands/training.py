"""Training export CLI commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from bcn.cli_commands.shared import build_settings
from bcn.cli_commands.shared import run_async


def register_training_commands(cli: click.Group) -> None:
    """Attach training export commands to the root CLI group."""

    @cli.command("export-training")
    @click.option("--output-dir", default="training_export", show_default=True)
    @click.option(
        "--limit",
        type=int,
        default=0,
        show_default=True,
        help="Max runs to export (0=all).",
    )
    @click.option(
        "--since-days",
        type=int,
        default=0,
        show_default=True,
        help="Only runs from last N days.",
    )
    @click.option(
        "--include-blocked/--published-only",
        default=False,
        show_default=True,
        help="Include blocked generations in exports.",
    )
    @click.option(
        "--include-shadow-preferences/--generation-only",
        default=True,
        show_default=True,
        help="Include high-confidence shadow lane preference rows and raw shadow traces.",
    )
    def export_training(
        output_dir: str,
        limit: int,
        since_days: int,
        include_blocked: bool,
        include_shadow_preferences: bool,
    ) -> None:
        """Export SFT + preference JSONL datasets from stored traces."""
        settings = build_settings()

        async def _run() -> None:
            from datetime import datetime
            from datetime import timezone
            from uuid import UUID

            from bcn.evaluation import build_shadow_preference_pair
            from bcn.persistence.evaluation import get_evaluation_runs_for_export
            from bcn.persistence.optimization import (
                get_optimization_candidate_lane_results,
            )
            from bcn.persistence.optimization import (
                get_optimization_candidates_for_export,
            )
            from bcn.persistence.runtime import close_pool
            from bcn.persistence.runtime import get_pool
            from bcn.persistence.training import get_distribution_outcomes
            from bcn.persistence.training import get_generation_preference_pairs_for_runs
            from bcn.persistence.training import get_generation_rounds_for_runs
            from bcn.persistence.training import get_generation_runs_for_export
            from bcn.persistence.training import get_human_reviews

            def _iso(value: Any) -> str | None:
                if isinstance(value, UUID):
                    return str(value)
                if hasattr(value, "isoformat"):
                    return value.isoformat()
                return str(value) if value is not None else None

            def _json_safe(value: Any) -> Any:
                if isinstance(value, UUID):
                    return str(value)
                if hasattr(value, "isoformat"):
                    return value.isoformat()
                if isinstance(value, dict):
                    return {str(k): _json_safe(v) for k, v in value.items()}
                if isinstance(value, list):
                    return [_json_safe(v) for v in value]
                return value

            def _normalize_json(value: Any, default: Any) -> Any:
                if isinstance(value, type(default)):
                    return value
                if isinstance(value, str):
                    try:
                        parsed = json.loads(value)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        return default
                    if isinstance(parsed, type(default)):
                        return parsed
                return default

            await get_pool(settings)
            runs = await get_generation_runs_for_export(
                limit=max(0, int(limit)),
                since_days=max(0, int(since_days)),
                include_blocked=include_blocked,
            )
            shadow_runs = (
                await get_evaluation_runs_for_export(
                    lane="shadow",
                    limit=max(0, int(limit)),
                    since_days=max(0, int(since_days)),
                )
                if include_shadow_preferences
                else []
            )
            optimization_candidates = await get_optimization_candidates_for_export(
                limit=max(0, int(limit)),
                since_days=max(0, int(since_days)),
            )
            optimization_candidate_ids: list[UUID] = [
                row["id"] for row in optimization_candidates
            ]
            optimization_lane_rows = (
                await get_optimization_candidate_lane_results(
                    optimization_candidate_ids
                )
                if optimization_candidate_ids
                else []
            )
            if not runs and not shadow_runs and not optimization_candidates:
                click.echo("No generation runs found for export")
                await close_pool()
                return

            run_ids: list[UUID] = [row["id"] for row in runs]
            briefing_ids: list[UUID] = [
                row["briefing_id"] for row in runs if row["briefing_id"]
            ]
            rounds = await get_generation_rounds_for_runs(run_ids)
            prefs = await get_generation_preference_pairs_for_runs(run_ids)
            reviews = await get_human_reviews(run_ids=run_ids)
            outcomes = (
                await get_distribution_outcomes(briefing_ids=briefing_ids)
                if briefing_ids
                else []
            )

            rounds_by_run: dict[str, list[dict[str, Any]]] = {}
            for row in rounds:
                run_key = str(row["run_id"])
                rounds_by_run.setdefault(run_key, []).append(dict(row))

            reviews_by_run: dict[str, list[dict[str, Any]]] = {}
            reviews_by_briefing: dict[str, list[dict[str, Any]]] = {}
            for row in reviews:
                payload = dict(row)
                run_key = str(payload["run_id"]) if payload.get("run_id") else ""
                briefing_key = (
                    str(payload["briefing_id"]) if payload.get("briefing_id") else ""
                )
                if run_key:
                    reviews_by_run.setdefault(run_key, []).append(payload)
                if briefing_key:
                    reviews_by_briefing.setdefault(briefing_key, []).append(payload)

            outcomes_by_briefing: dict[str, list[dict[str, Any]]] = {}
            for row in outcomes:
                raw_payload = dict(row)
                payload = _json_safe(raw_payload)
                briefing_key = (
                    str(payload["briefing_id"]) if payload.get("briefing_id") else ""
                )
                if briefing_key:
                    outcomes_by_briefing.setdefault(briefing_key, []).append(payload)

            for payloads in reviews_by_run.values():
                payloads.sort(key=lambda row: row.get("created_at"), reverse=True)
            for payloads in reviews_by_briefing.values():
                payloads.sort(key=lambda row: row.get("created_at"), reverse=True)

            out_dir = Path(output_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            sft_path = out_dir / "sft.jsonl"
            pref_path = out_dir / "preference.jsonl"
            trace_path = out_dir / "trace_runs.jsonl"
            shadow_trace_path = out_dir / "shadow_trace.jsonl"
            optimization_trace_path = out_dir / "optimization_trace.jsonl"
            manifest_path = out_dir / "manifest.json"

            sft_rows: list[dict[str, Any]] = []
            trace_rows: list[dict[str, Any]] = []
            for run in runs:
                run_dict = dict(run)
                run_key = str(run_dict["id"])
                briefing_key = (
                    str(run_dict["briefing_id"]) if run_dict.get("briefing_id") else ""
                )
                selected_items = _normalize_json(run_dict.get("selected_items"), [])
                prompts = _normalize_json(run_dict.get("prompts"), {})
                config_snapshot = _normalize_json(run_dict.get("config_snapshot"), {})
                run_reviews = reviews_by_run.get(run_key, [])
                briefing_reviews = reviews_by_briefing.get(briefing_key, [])
                latest_review = (run_reviews or briefing_reviews or [None])[0]

                target_markdown = str(run_dict.get("final_draft") or "").strip()
                if latest_review and latest_review.get("edited_markdown"):
                    decision = str(latest_review.get("decision") or "").lower()
                    if decision in {"edit", "accept"}:
                        target_markdown = (
                            str(latest_review["edited_markdown"]).strip()
                            or target_markdown
                        )

                if target_markdown:
                    sft_rows.append(
                        {
                            "id": run_key,
                            "briefing_id": briefing_key or None,
                            "decision": str(run_dict.get("decision") or ""),
                            "mode": str(run_dict.get("mode") or "standard"),
                            "input": {
                                "selected_items": selected_items,
                                "prompt_versions": prompts,
                            },
                            "output_markdown": target_markdown,
                            "messages": [
                                {
                                    "role": "user",
                                    "content": json.dumps(
                                        {
                                            "mode": str(run_dict.get("mode") or "standard"),
                                            "selected_items": selected_items,
                                            "prompt_versions": prompts,
                                        },
                                        ensure_ascii=False,
                                    ),
                                },
                                {"role": "assistant", "content": target_markdown},
                            ],
                            "metadata": {
                                "created_at": _iso(run_dict.get("created_at")),
                                "rewrite_count": int(run_dict.get("rewrite_count") or 0),
                                "llm_model": run_dict.get("llm_model"),
                                "llm_model_version": run_dict.get("llm_model_version"),
                                "git_sha": run_dict.get("git_sha"),
                                "review_decision": (
                                    latest_review.get("decision")
                                    if latest_review
                                    else None
                                ),
                                "distribution_outcomes": outcomes_by_briefing.get(
                                    briefing_key, []
                                ),
                                "config_snapshot": config_snapshot,
                            },
                        }
                    )

                trace_rows.append(
                    {
                        "run_id": run_key,
                        "briefing_id": briefing_key or None,
                        "created_at": _iso(run_dict.get("created_at")),
                        "decision": run_dict.get("decision"),
                        "decision_reason": run_dict.get("decision_reason"),
                        "rewrite_count": int(run_dict.get("rewrite_count") or 0),
                        "llm_model": run_dict.get("llm_model"),
                        "llm_model_version": run_dict.get("llm_model_version"),
                        "git_sha": run_dict.get("git_sha"),
                        "selected_items": selected_items,
                        "prompt_versions": prompts,
                        "config_snapshot": config_snapshot,
                        "initial_draft": run_dict.get("initial_draft"),
                        "final_draft": run_dict.get("final_draft"),
                        "final_gate": _normalize_json(run_dict.get("final_gate"), {}),
                        "final_critique": _normalize_json(
                            run_dict.get("final_critique"), {}
                        ),
                        "final_verifier": _normalize_json(
                            run_dict.get("final_verifier"), {}
                        ),
                        "rounds": rounds_by_run.get(run_key, []),
                        "human_reviews": run_reviews or briefing_reviews,
                        "distribution_outcomes": outcomes_by_briefing.get(
                            briefing_key, []
                        ),
                    }
                )

            pref_rows: list[dict[str, Any]] = []
            run_lookup = {str(dict(run)["id"]): dict(run) for run in runs}
            for row in prefs:
                payload = dict(row)
                run_key = str(payload["run_id"])
                run_context = run_lookup.get(run_key, {})
                pref_rows.append(
                    {
                        "id": int(payload["id"]),
                        "run_id": run_key,
                        "source": str(payload.get("source") or "auto_writer_loop"),
                        "round_index": int(payload.get("round_index") or 0),
                        "chosen": str(payload.get("chosen_text") or ""),
                        "rejected": str(payload.get("rejected_text") or ""),
                        "rationale": str(payload.get("rationale") or ""),
                        "context": {
                            "mode": str(run_context.get("mode") or "standard"),
                            "selected_items": _normalize_json(
                                run_context.get("selected_items"), []
                            ),
                            "prompt_versions": _normalize_json(
                                run_context.get("prompts"), {}
                            ),
                        },
                        "metadata": {
                            "created_at": _iso(payload.get("created_at")),
                            "briefing_id": (
                                str(run_context.get("briefing_id"))
                                if run_context.get("briefing_id")
                                else None
                            ),
                        },
                    }
                )

            for review_list in reviews_by_run.values():
                for review_row in review_list:
                    run_id_raw = review_row.get("run_id")
                    if not run_id_raw:
                        continue
                    run_key = str(run_id_raw)
                    run_context = run_lookup.get(run_key, {})
                    edited = str(review_row.get("edited_markdown") or "").strip()
                    final = str(run_context.get("final_draft") or "").strip()
                    if not edited or not final or edited == final:
                        continue
                    decision = str(review_row.get("decision") or "").lower()
                    if decision not in {"edit", "accept"}:
                        continue
                    pref_rows.append(
                        {
                            "id": f"human-{review_row.get('id')}",
                            "run_id": run_key,
                            "source": "human_review",
                            "round_index": -1,
                            "chosen": edited,
                            "rejected": final,
                            "rationale": str(
                                review_row.get("notes")
                                or "human edited preferred variant"
                            ),
                            "context": {
                                "mode": str(run_context.get("mode") or "standard"),
                                "selected_items": _normalize_json(
                                    run_context.get("selected_items"), []
                                ),
                                "prompt_versions": _normalize_json(
                                    run_context.get("prompts"), {}
                                ),
                            },
                            "metadata": {
                                "review_id": str(review_row.get("id")),
                                "created_at": _iso(review_row.get("created_at")),
                            },
                        }
                    )

            shadow_trace_rows: list[dict[str, Any]] = []
            shadow_preference_rows = 0
            for row in shadow_runs:
                row_dict = dict(row)
                report = _normalize_json(row_dict.get("report"), {})
                summary = _normalize_json(row_dict.get("summary"), {})
                if report and "summary" not in report:
                    report["summary"] = summary
                trace_row = {
                    "shadow_run_id": str(row_dict.get("id")),
                    "created_at": _iso(row_dict.get("created_at")),
                    "generated_at": _iso(row_dict.get("generated_at")),
                    "workflow_mode": str(row_dict.get("workflow_mode") or ""),
                    "candidate_overrides": _normalize_json(
                        row_dict.get("candidate_overrides"), {}
                    ),
                    "summary": summary,
                    "report": report,
                }
                shadow_trace_rows.append(trace_row)

                pair = build_shadow_preference_pair(report)
                if not pair:
                    continue
                pref_rows.append(
                    {
                        "id": f"shadow-{row_dict.get('id')}",
                        "run_id": str(row_dict.get("id")),
                        "source": "shadow_lane",
                        "round_index": 0,
                        "chosen": pair["chosen"],
                        "rejected": pair["rejected"],
                        "rationale": pair["rationale"],
                        "context": {
                            **pair["context"],
                            "candidate_overrides": _normalize_json(
                                row_dict.get("candidate_overrides"), {}
                            ),
                        },
                        "metadata": {
                            "created_at": _iso(row_dict.get("created_at")),
                            "generated_at": _iso(row_dict.get("generated_at")),
                            "workflow_mode": str(row_dict.get("workflow_mode") or ""),
                            "preferred_side": pair["preferred_side"],
                            "recommendation": pair["recommendation"],
                            "confidence": pair["confidence"],
                            "selection_overlap_ratio": pair["selection_overlap_ratio"],
                        },
                    }
                )
                shadow_preference_rows += 1

            optimization_trace_rows: list[dict[str, Any]] = []
            optimization_preference_rows = 0
            lane_rows_by_candidate: dict[str, list[dict[str, Any]]] = {}
            for row in optimization_lane_rows:
                payload = dict(row)
                key = str(payload.get("optimization_candidate_id") or "")
                if key:
                    lane_rows_by_candidate.setdefault(key, []).append(payload)

            def _build_benchmark_pref_rows(
                candidate_row: dict[str, Any],
                benchmark_report: dict[str, Any],
            ) -> list[dict[str, Any]]:
                rows: list[dict[str, Any]] = []
                results = benchmark_report.get("results", [])
                if not isinstance(results, list):
                    return rows
                for idx, raw_case in enumerate(results):
                    if not isinstance(raw_case, dict):
                        continue
                    champion = raw_case.get("champion", {})
                    candidate = raw_case.get("candidate", {})
                    if not isinstance(champion, dict) or not isinstance(candidate, dict):
                        continue
                    champion_text = str(champion.get("markdown") or "").strip()
                    candidate_text = str(candidate.get("markdown") or "").strip()
                    if not champion_text or not candidate_text or champion_text == candidate_text:
                        continue
                    champion_case_pass = bool(champion.get("case_pass"))
                    candidate_case_pass = bool(candidate.get("case_pass"))
                    champion_score = int(champion.get("rubric", {}).get("score", 0) or 0)
                    candidate_score = int(candidate.get("rubric", {}).get("score", 0) or 0)
                    prefer_candidate: bool | None = None
                    rationale = ""
                    if candidate_case_pass and not champion_case_pass:
                        prefer_candidate = True
                        rationale = "optimization_benchmark candidate passed case while champion failed"
                    elif champion_case_pass and not candidate_case_pass:
                        prefer_candidate = False
                        rationale = "optimization_benchmark champion passed case while candidate failed"
                    elif abs(candidate_score - champion_score) >= 3:
                        prefer_candidate = candidate_score > champion_score
                        rationale = (
                            "optimization_benchmark "
                            f"score_delta={candidate_score - champion_score}"
                        )
                    if prefer_candidate is None:
                        continue
                    chosen = candidate_text if prefer_candidate else champion_text
                    rejected = champion_text if prefer_candidate else candidate_text
                    rows.append(
                        {
                            "id": f"optimization-benchmark-{candidate_row['id']}-{idx}",
                            "run_id": str(candidate_row["optimization_run_id"]),
                            "source": "optimization_benchmark",
                            "round_index": idx,
                            "chosen": chosen,
                            "rejected": rejected,
                            "rationale": rationale,
                            "context": {
                                "variant_id": str(candidate_row.get("variant_id") or ""),
                                "selected_items": _normalize_json(
                                    raw_case.get("selected_items"), []
                                ),
                                "history": _normalize_json(raw_case.get("history"), []),
                                "issue_tags": _normalize_json(raw_case.get("issue_tags"), []),
                            },
                            "metadata": {
                                "candidate_id": str(candidate_row["id"]),
                                "created_at": _iso(candidate_row.get("created_at")),
                                "expected_decision": str(raw_case.get("expected_decision") or ""),
                                "preferred_side": "candidate" if prefer_candidate else "champion",
                                "champion_score": champion_score,
                                "candidate_score": candidate_score,
                            },
                        }
                    )
                return rows

            def _build_replay_pref_rows(
                candidate_row: dict[str, Any],
                champion_report: dict[str, Any],
                candidate_report: dict[str, Any],
            ) -> list[dict[str, Any]]:
                rows: list[dict[str, Any]] = []
                champion_results = champion_report.get("results", [])
                candidate_results = candidate_report.get("results", [])
                if not isinstance(champion_results, list) or not isinstance(candidate_results, list):
                    return rows
                champion_by_briefing = {
                    str(row.get("briefing_id") or ""): row
                    for row in champion_results
                    if isinstance(row, dict) and str(row.get("briefing_id") or "")
                }
                for candidate_case in candidate_results:
                    if not isinstance(candidate_case, dict):
                        continue
                    briefing_id = str(candidate_case.get("briefing_id") or "")
                    if not briefing_id:
                        continue
                    champion_case = champion_by_briefing.get(briefing_id)
                    if not isinstance(champion_case, dict):
                        continue
                    champion_text = str(champion_case.get("simulated_markdown") or "").strip()
                    candidate_text = str(candidate_case.get("simulated_markdown") or "").strip()
                    if not champion_text or not candidate_text or champion_text == candidate_text:
                        continue
                    champion_score = int(champion_case.get("simulated_score", 0) or 0)
                    candidate_score = int(candidate_case.get("simulated_score", 0) or 0)
                    if abs(candidate_score - champion_score) < 3:
                        continue
                    prefer_candidate = candidate_score > champion_score
                    chosen = candidate_text if prefer_candidate else champion_text
                    rejected = champion_text if prefer_candidate else candidate_text
                    rows.append(
                        {
                            "id": f"optimization-replay-{candidate_row['id']}-{briefing_id}",
                            "run_id": str(candidate_row["optimization_run_id"]),
                            "source": "optimization_replay",
                            "round_index": 0,
                            "chosen": chosen,
                            "rejected": rejected,
                            "rationale": (
                                "optimization_replay "
                                f"score_delta={candidate_score - champion_score}"
                            ),
                            "context": {
                                "variant_id": str(candidate_row.get("variant_id") or ""),
                                "selected_items": _normalize_json(
                                    candidate_case.get("selected_items"), []
                                ),
                                "history": _normalize_json(candidate_case.get("history"), []),
                                "simulated_selected_items": _normalize_json(
                                    candidate_case.get("simulated_selected_items"),
                                    [],
                                ),
                            },
                            "metadata": {
                                "candidate_id": str(candidate_row["id"]),
                                "briefing_id": briefing_id,
                                "created_at": _iso(candidate_row.get("created_at")),
                                "preferred_side": "candidate" if prefer_candidate else "champion",
                                "champion_score": champion_score,
                                "candidate_score": candidate_score,
                            },
                        }
                    )
                return rows

            for row in optimization_candidates:
                candidate_payload = dict(row)
                candidate_key = str(candidate_payload["id"])
                lane_rows = lane_rows_by_candidate.get(candidate_key, [])
                lane_payloads = {
                    str(item.get("lane") or ""): _normalize_json(item.get("report"), {})
                    for item in lane_rows
                }
                trace_row = {
                    "candidate_id": candidate_key,
                    "optimization_run_id": str(candidate_payload["optimization_run_id"]),
                    "variant_id": str(candidate_payload.get("variant_id") or ""),
                    "base_variant": str(candidate_payload.get("base_variant") or ""),
                    "created_at": _iso(candidate_payload.get("created_at")),
                    "git_sha": str(candidate_payload.get("git_sha") or ""),
                    "benchmark_pack_path": str(
                        candidate_payload.get("benchmark_pack_path") or ""
                    ),
                    "variant_payload": _normalize_json(
                        candidate_payload.get("variant_payload"), {}
                    ),
                    "summary": _normalize_json(candidate_payload.get("summary"), {}),
                    "lane_reports": lane_payloads,
                }
                optimization_trace_rows.append(trace_row)

                benchmark_report = lane_payloads.get("benchmark", {})
                champion_report = lane_payloads.get("replay_champion", {})
                candidate_report = lane_payloads.get("replay_candidate", {})
                for pref_row in _build_benchmark_pref_rows(
                    candidate_payload,
                    benchmark_report if isinstance(benchmark_report, dict) else {},
                ):
                    pref_rows.append(pref_row)
                    optimization_preference_rows += 1
                for pref_row in _build_replay_pref_rows(
                    candidate_payload,
                    champion_report if isinstance(champion_report, dict) else {},
                    candidate_report if isinstance(candidate_report, dict) else {},
                ):
                    pref_rows.append(pref_row)
                    optimization_preference_rows += 1

            with sft_path.open("w", encoding="utf-8") as handle:
                for row in sft_rows:
                    handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            with pref_path.open("w", encoding="utf-8") as handle:
                for row in pref_rows:
                    handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            with trace_path.open("w", encoding="utf-8") as handle:
                for row in trace_rows:
                    handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            with shadow_trace_path.open("w", encoding="utf-8") as handle:
                for row in shadow_trace_rows:
                    handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            with optimization_trace_path.open("w", encoding="utf-8") as handle:
                for row in optimization_trace_rows:
                    handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

            manifest = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "run_count": len(runs),
                "sft_rows": len(sft_rows),
                "preference_rows": len(pref_rows),
                "trace_rows": len(trace_rows),
                "shadow_trace_rows": len(shadow_trace_rows),
                "shadow_preference_rows": shadow_preference_rows,
                "optimization_trace_rows": len(optimization_trace_rows),
                "optimization_preference_rows": optimization_preference_rows,
                "filters": {
                    "limit": int(limit),
                    "since_days": int(since_days),
                    "include_blocked": bool(include_blocked),
                    "include_shadow_preferences": bool(include_shadow_preferences),
                },
                "files": {
                    "sft_jsonl": str(sft_path),
                    "preference_jsonl": str(pref_path),
                    "trace_jsonl": str(trace_path),
                    "shadow_trace_jsonl": str(shadow_trace_path),
                    "optimization_trace_jsonl": str(optimization_trace_path),
                },
            }
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            click.echo(
                f"Export complete: runs={len(runs)} sft_rows={len(sft_rows)} "
                f"preference_rows={len(pref_rows)} shadow_preference_rows={shadow_preference_rows} "
                f"optimization_preference_rows={optimization_preference_rows}"
            )
            click.echo(f"  SFT: {sft_path}")
            click.echo(f"  Preference: {pref_path}")
            click.echo(f"  Traces: {trace_path}")
            click.echo(f"  Shadow Traces: {shadow_trace_path}")
            click.echo(f"  Optimization Traces: {optimization_trace_path}")
            click.echo(f"  Manifest: {manifest_path}")
            await close_pool()

        run_async(_run)


__all__ = ["register_training_commands"]
