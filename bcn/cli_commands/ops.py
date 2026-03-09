"""Review, export, history, and subscriber CLI commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from bcn.cli_commands.shared import build_settings
from bcn.cli_commands.shared import run_async


def register_ops_commands(cli: click.Group) -> None:
    """Attach operator/admin CLI commands to the root Click group."""

    @cli.group("newsletter-subscribers")
    def newsletter_subscribers() -> None:
        """Manage monthly newsletter email subscribers."""

    @newsletter_subscribers.command("list")
    @click.option(
        "--all",
        "include_inactive",
        is_flag=True,
        help="Include inactive subscribers.",
    )
    def newsletter_subscribers_list(include_inactive: bool) -> None:
        """List newsletter subscribers from the database."""
        settings = build_settings()

        async def _run() -> None:
            from bcn.persistence.newsletter import get_newsletter_subscribers
            from bcn.persistence.runtime import close_pool
            from bcn.persistence.runtime import get_pool

            await get_pool(settings)
            rows = await get_newsletter_subscribers(active_only=not include_inactive)
            if not rows:
                click.echo("No newsletter subscribers found")
                await close_pool()
                return

            for row in rows:
                payload = dict(row)
                status = "active" if payload.get("is_active") else "inactive"
                click.echo(
                    f"{payload.get('email')} | status={status} "
                    f"| updated_at={payload.get('updated_at').isoformat()}"
                )
            await close_pool()

        run_async(_run)

    @newsletter_subscribers.command("add")
    @click.argument("email", type=str)
    def newsletter_subscribers_add(email: str) -> None:
        """Add or reactivate a newsletter subscriber."""
        settings = build_settings()

        async def _run() -> None:
            from bcn.persistence.newsletter import add_newsletter_subscriber
            from bcn.persistence.runtime import close_pool
            from bcn.persistence.runtime import get_pool

            await get_pool(settings)
            try:
                inserted = await add_newsletter_subscriber(email)
            except ValueError as exc:
                raise click.ClickException(str(exc)) from exc
            click.echo(
                f"{'Added' if inserted else 'Reactivated'} newsletter subscriber: "
                f"{email.strip().lower()}"
            )
            await close_pool()

        run_async(_run)

    @newsletter_subscribers.command("remove")
    @click.argument("email", type=str)
    def newsletter_subscribers_remove(email: str) -> None:
        """Deactivate a newsletter subscriber."""
        settings = build_settings()

        async def _run() -> None:
            from bcn.persistence.newsletter import remove_newsletter_subscriber
            from bcn.persistence.runtime import close_pool
            from bcn.persistence.runtime import get_pool

            await get_pool(settings)
            removed = await remove_newsletter_subscriber(email)
            if removed:
                click.echo(f"Removed newsletter subscriber: {email.strip().lower()}")
            else:
                click.echo(
                    f"Subscriber not found or already inactive: {email.strip().lower()}"
                )
            await close_pool()

        run_async(_run)

    @cli.command("review")
    @click.option(
        "--briefing-id", type=str, help="Briefing UUID to review (defaults to latest)."
    )
    @click.option(
        "--decision",
        type=click.Choice(["accept", "reject", "edit", "needs_work"]),
        required=True,
        help="Human review decision label.",
    )
    @click.option(
        "--issue-tag",
        "issue_tags",
        multiple=True,
        help="Issue tag (repeatable).",
    )
    @click.option("--edited-file", type=click.Path(exists=True, dir_okay=False))
    @click.option("--edited-text", type=str, help="Edited markdown text.")
    @click.option("--notes", type=str, help="Free-form reviewer notes.")
    @click.option("--reviewer", type=str, default="cli", show_default=True)
    def review(
        briefing_id: str | None,
        decision: str,
        issue_tags: tuple[str, ...],
        edited_file: str | None,
        edited_text: str | None,
        notes: str | None,
        reviewer: str,
    ) -> None:
        """Store human feedback labels/edits for one briefing."""
        settings = build_settings()

        async def _run() -> None:
            from uuid import UUID

            from bcn.persistence.briefings import get_briefing_by_id
            from bcn.persistence.briefings import get_latest_any_briefing
            from bcn.persistence.runtime import close_pool
            from bcn.persistence.runtime import get_pool
            from bcn.persistence.training import get_latest_generation_run_for_briefing
            from bcn.persistence.training import insert_human_review

            if edited_file and edited_text:
                raise click.ClickException(
                    "Use either --edited-file or --edited-text, not both."
                )

            parsed_id = None
            if briefing_id:
                try:
                    parsed_id = UUID(briefing_id)
                except ValueError as exc:
                    raise click.ClickException(
                        f"Invalid briefing UUID: {briefing_id}"
                    ) from exc

            await get_pool(settings)
            briefing = (
                await get_briefing_by_id(parsed_id)
                if parsed_id
                else await get_latest_any_briefing()
            )
            if not briefing:
                click.echo("No briefing found to review")
                await close_pool()
                return

            edited_markdown = edited_text
            if edited_file:
                edited_markdown = Path(edited_file).read_text(encoding="utf-8")

            run = await get_latest_generation_run_for_briefing(briefing["id"])
            run_id = run["id"] if run else None
            review_id = await insert_human_review(
                briefing_id=briefing["id"],
                run_id=run_id,
                decision=decision,
                issue_tags=list(issue_tags),
                reviewer=reviewer,
                edited_markdown=edited_markdown,
                notes=notes,
            )
            click.echo(
                f"Stored review {review_id} for briefing {briefing['id']} "
                f"(decision={decision}, tags={len(issue_tags)})"
            )
            await close_pool()

        run_async(_run)

    @cli.command("review-queue")
    @click.option("--limit", type=int, default=20, show_default=True)
    @click.option(
        "--only-unreviewed",
        is_flag=True,
        help="Show only briefings without reviews.",
    )
    def review_queue(limit: int, only_unreviewed: bool) -> None:
        """List recent briefings and review status."""
        settings = build_settings()

        async def _run() -> None:
            from bcn.persistence.runtime import close_pool
            from bcn.persistence.runtime import get_pool
            from bcn.persistence.training import get_review_queue

            await get_pool(settings)
            rows = await get_review_queue(
                limit=max(1, int(limit)),
                only_unreviewed=only_unreviewed,
            )
            if not rows:
                click.echo("No briefings in review queue")
                await close_pool()
                return

            for row in rows:
                payload = dict(row)
                click.echo(
                    f"{payload['id']} | status={payload['status']} | reviews={payload['review_count']} "
                    f"| last_decision={payload['last_decision'] or '-'} | created_at={payload['created_at'].isoformat()}"
                )
                preview = str(payload.get("preview") or "").replace("\n", " ")
                if preview:
                    click.echo(f"  preview: {preview[:160]}")
            await close_pool()

        run_async(_run)

    @cli.command("record-outcome")
    @click.option("--briefing-id", required=True, help="Briefing UUID.")
    @click.option(
        "--channel", required=True, help="Channel name (telegram/email/slack/etc)."
    )
    @click.option("--status", default="ok", show_default=True)
    @click.option("--message-id", type=str, help="External message/post id.")
    @click.option("--post-url", type=str, help="External post URL.")
    @click.option("--views", type=int, help="View count metric.")
    @click.option("--reactions", type=int, help="Reaction count metric.")
    @click.option("--clicks", type=int, help="Click count metric.")
    @click.option("--link-clicks", type=str, help="JSON object with per-link clicks.")
    @click.option("--metadata", type=str, help="JSON object with extra metadata.")
    def record_outcome(
        briefing_id: str,
        channel: str,
        status: str,
        message_id: str | None,
        post_url: str | None,
        views: int | None,
        reactions: int | None,
        clicks: int | None,
        link_clicks: str | None,
        metadata: str | None,
    ) -> None:
        """Upsert distribution outcome metrics linked to a briefing."""
        settings = build_settings()

        async def _run() -> None:
            from uuid import UUID

            from bcn.persistence.runtime import close_pool
            from bcn.persistence.runtime import get_pool
            from bcn.persistence.training import upsert_distribution_outcome

            try:
                parsed_id = UUID(briefing_id)
            except ValueError as exc:
                raise click.ClickException(
                    f"Invalid briefing UUID: {briefing_id}"
                ) from exc

            link_clicks_payload: dict[str, Any] = {}
            if link_clicks:
                try:
                    parsed_clicks = json.loads(link_clicks)
                except json.JSONDecodeError as exc:
                    raise click.ClickException(
                        f"--link-clicks must be valid JSON: {exc}"
                    ) from exc
                if isinstance(parsed_clicks, dict):
                    link_clicks_payload = parsed_clicks

            metadata_payload: dict[str, Any] = {}
            if metadata:
                try:
                    parsed_meta = json.loads(metadata)
                except json.JSONDecodeError as exc:
                    raise click.ClickException(
                        f"--metadata must be valid JSON: {exc}"
                    ) from exc
                if isinstance(parsed_meta, dict):
                    metadata_payload = parsed_meta

            metrics: dict[str, Any] = {}
            if views is not None:
                metrics["views"] = int(views)
            if reactions is not None:
                metrics["reactions"] = int(reactions)
            if clicks is not None:
                metrics["clicks"] = int(clicks)
            if link_clicks_payload:
                metrics["link_clicks"] = link_clicks_payload

            await get_pool(settings)
            await upsert_distribution_outcome(
                briefing_id=parsed_id,
                channel=channel,
                status=status,
                external_message_id=message_id,
                external_post_url=post_url,
                metrics=metrics,
                metadata=metadata_payload,
            )
            click.echo(
                f"Stored distribution outcome for briefing {parsed_id} channel={channel} status={status}"
            )
            await close_pool()

        run_async(_run)

    @cli.command("import-history")
    @click.option(
        "--file",
        "file_path",
        type=click.Path(exists=True, dir_okay=False, path_type=Path),
        required=True,
        help="Path to channel export text (Name, [M/D/YYYY H:MM AM] format).",
    )
    @click.option(
        "--channel",
        default="telegram",
        show_default=True,
        help="Distribution channel label for imported history.",
    )
    @click.option(
        "--timezone",
        default="",
        help="Timezone used in the export timestamps (default: BCN_DISTRIBUTE_TIMEZONE).",
    )
    @click.option("--dry-run", is_flag=True, help="Parse and report only, no DB writes.")
    def import_history(
        file_path: Path,
        channel: str,
        timezone: str,
        dry_run: bool,
    ) -> None:
        """Backfill previously published channel posts into DB history."""
        settings = build_settings()

        async def _run() -> None:
            from bcn.history import extract_unique_post_urls
            from bcn.history import parse_channel_history_text
            from bcn.persistence.history import import_channel_history_posts
            from bcn.persistence.runtime import close_pool
            from bcn.persistence.runtime import get_pool

            tz_name = (timezone or settings.distribute_timezone or "UTC").strip()
            try:
                raw_text = file_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise click.ClickException(f"Failed to read {file_path}: {exc}") from exc

            try:
                parsed_posts = parse_channel_history_text(raw_text, timezone_name=tz_name)
            except Exception as exc:
                raise click.ClickException(
                    f"Failed to parse history file with timezone '{tz_name}': {exc}"
                ) from exc

            if not parsed_posts:
                click.echo("No posts found in file.")
                return

            payload: list[dict[str, Any]] = []
            unique_urls: set[str] = set()
            for post in parsed_posts:
                urls = extract_unique_post_urls(post.content_markdown)
                unique_urls.update(urls)
                payload.append(
                    {
                        "author": post.author,
                        "posted_at": post.posted_at,
                        "content_markdown": post.content_markdown,
                        "content_hash": post.content_hash,
                        "urls": urls,
                    }
                )

            earliest = min(post.posted_at for post in parsed_posts)
            latest = max(post.posted_at for post in parsed_posts)
            click.echo(
                "Parsed "
                f"{len(parsed_posts)} posts ({len(unique_urls)} unique URLs), "
                f"range={earliest.isoformat()}..{latest.isoformat()}, tz={tz_name}"
            )

            if dry_run:
                click.echo("Dry-run only; no database changes applied.")
                return

            await get_pool(settings)
            try:
                stats = await import_channel_history_posts(
                    channel=channel,
                    posts=payload,
                )
                click.echo(
                    "History import complete: "
                    f"posts inserted={stats['inserted_posts']}, "
                    f"posts existing={stats['existing_posts']}, "
                    f"urls inserted={stats['inserted_urls']}, "
                    f"urls existing={stats['existing_urls']}, "
                    f"posts skipped={stats['skipped_posts']}"
                )
            finally:
                await close_pool()

        run_async(_run)

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
            if not runs and not shadow_runs:
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
                briefing_key = str(payload["briefing_id"]) if payload.get("briefing_id") else ""
                if run_key:
                    reviews_by_run.setdefault(run_key, []).append(payload)
                if briefing_key:
                    reviews_by_briefing.setdefault(briefing_key, []).append(payload)

            outcomes_by_briefing: dict[str, list[dict[str, Any]]] = {}
            for row in outcomes:
                raw_payload = dict(row)
                payload = _json_safe(raw_payload)
                briefing_key = str(payload["briefing_id"]) if payload.get("briefing_id") else ""
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
            manifest_path = out_dir / "manifest.json"

            sft_rows: list[dict[str, Any]] = []
            trace_rows: list[dict[str, Any]] = []
            for run in runs:
                run_dict = dict(run)
                run_key = str(run_dict["id"])
                briefing_key = str(run_dict["briefing_id"]) if run_dict.get("briefing_id") else ""
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
                        target_markdown = str(latest_review["edited_markdown"]).strip() or target_markdown

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
                                "review_decision": latest_review.get("decision") if latest_review else None,
                                "distribution_outcomes": outcomes_by_briefing.get(briefing_key, []),
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
                        "final_critique": _normalize_json(run_dict.get("final_critique"), {}),
                        "final_verifier": _normalize_json(run_dict.get("final_verifier"), {}),
                        "rounds": rounds_by_run.get(run_key, []),
                        "human_reviews": run_reviews or briefing_reviews,
                        "distribution_outcomes": outcomes_by_briefing.get(briefing_key, []),
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
                            "selected_items": _normalize_json(run_context.get("selected_items"), []),
                            "prompt_versions": _normalize_json(run_context.get("prompts"), {}),
                        },
                        "metadata": {
                            "created_at": _iso(payload.get("created_at")),
                            "briefing_id": (
                                str(run_context.get("briefing_id")) if run_context.get("briefing_id") else None
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
                                review_row.get("notes") or "human edited preferred variant"
                            ),
                            "context": {
                                "mode": str(run_context.get("mode") or "standard"),
                                "selected_items": _normalize_json(run_context.get("selected_items"), []),
                                "prompt_versions": _normalize_json(run_context.get("prompts"), {}),
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
                    "candidate_overrides": _normalize_json(row_dict.get("candidate_overrides"), {}),
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

            manifest = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "run_count": len(runs),
                "sft_rows": len(sft_rows),
                "preference_rows": len(pref_rows),
                "trace_rows": len(trace_rows),
                "shadow_trace_rows": len(shadow_trace_rows),
                "shadow_preference_rows": shadow_preference_rows,
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
                },
            }
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            click.echo(
                f"Export complete: runs={len(runs)} sft_rows={len(sft_rows)} "
                f"preference_rows={len(pref_rows)} shadow_preference_rows={shadow_preference_rows}"
            )
            click.echo(f"  SFT: {sft_path}")
            click.echo(f"  Preference: {pref_path}")
            click.echo(f"  Traces: {trace_path}")
            click.echo(f"  Shadow Traces: {shadow_trace_path}")
            click.echo(f"  Manifest: {manifest_path}")
            await close_pool()

        run_async(_run)

    @cli.command("finalize-pending-runs")
    @click.option(
        "--max-age-minutes",
        type=int,
        default=180,
        show_default=True,
        help="Only finalize PENDING generation runs older than this threshold.",
    )
    @click.option(
        "--decision",
        type=click.Choice(["blocked", "skipped"]),
        default="blocked",
        show_default=True,
        help="Decision label to set on stale PENDING runs.",
    )
    def finalize_pending_runs(max_age_minutes: int, decision: str) -> None:
        """Finalize stale PENDING generation runs to avoid dangling traces."""
        settings = build_settings()

        async def _run() -> None:
            from bcn.persistence.runtime import close_pool
            from bcn.persistence.runtime import get_pool
            from bcn.persistence.training import finalize_stale_pending_generation_runs

            await get_pool(settings)
            updated = await finalize_stale_pending_generation_runs(
                max_age_minutes=max(1, int(max_age_minutes)),
                decision=decision.upper(),
                decision_reason=f"manual_finalize_stale_pending_run:{decision.lower()}",
            )
            click.echo(
                f"Finalized {updated} stale PENDING generation runs as {decision.upper()}"
            )
            await close_pool()

        run_async(_run)


__all__ = ["register_ops_commands"]
