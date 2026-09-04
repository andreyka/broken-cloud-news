"""Selection planning helpers for writer workflows."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from bcn.contracts.modes import REGULAR_MONTHLY_NEWSLETTER_MODE
from bcn.contracts.modes import WEEKLY_FLAGSHIP_MODE


def _selection_item_summary(
    service: Any,
    item: dict[str, Any],
    *,
    recent_published: list[dict[str, Any]] | None = None,
    writer_item_ids: set[str] | None = None,
    selected_item_ids: set[str] | None = None,
) -> dict[str, Any]:
    selector = service.selector
    recent_items = list(recent_published or [])
    writer_ids = writer_item_ids or set()
    selected_ids = selected_item_ids or set()
    item_id = str(item.get("id") or "")
    source = str(item.get("source_type") or "").lower()
    relevance = int(item.get("relevance_score", 0) or 0)
    actionable = bool(selector.is_actionable(item))
    source_floor_passed = bool(selector.passes_source_floor(item))
    duplicate_recent = bool(selector.is_duplicate_of(item, recent_items))
    blocked_existing = bool(item.get("blocked_by_existing_briefing", False))
    in_writer_pool = item_id in writer_ids if item_id else False
    selected = item_id in selected_ids if item_id else False

    reasons: list[str] = []
    if blocked_existing:
        reasons.append("existing_briefing_story_duplicate")
    if in_writer_pool and not source_floor_passed and not selected:
        reasons.append("source_floor")
    if in_writer_pool and duplicate_recent and not selected:
        reasons.append("recent_briefing_novelty")

    return {
        "id": item_id or None,
        "title": str(item.get("title") or ""),
        "source_type": source,
        "relevance_score": relevance,
        "published_at": item.get("published_at"),
        "url": str(item.get("url") or ""),
        "story_issue_key": str(item.get("story_issue_key") or ""),
        "story_url_key": str(item.get("story_url_key") or ""),
        "in_writer_pool": in_writer_pool,
        "selected": selected,
        "actionable": actionable,
        "high_signal": bool(
            relevance >= int(service.settings.briefing_quiet_day_high_signal_threshold)
            and (actionable or source == "ghsa")
        ),
        "source_floor_passed": source_floor_passed,
        "blocked_by_existing_briefing": blocked_existing,
        "duplicate_recent_briefing": duplicate_recent,
        "exclusion_reasons": reasons,
    }


def build_selection_trace(
    service: Any,
    *,
    pool_items: list[dict[str, Any]],
    writer_items: list[dict[str, Any]],
    selected_items: list[dict[str, Any]],
    recent_published: list[dict[str, Any]] | None = None,
    workflow_mode: str,
    decision: str,
    reason: str,
    message: str,
    selection_mode: str,
) -> dict[str, Any]:
    """Return structured selection diagnostics for training/export."""
    writer_item_ids = {
        str(item.get("id") or "") for item in writer_items if str(item.get("id") or "")
    }
    selected_item_ids = {
        str(item.get("id") or "")
        for item in selected_items
        if str(item.get("id") or "")
    }
    summaries = [
        _selection_item_summary(
            service,
            item,
            recent_published=recent_published,
            writer_item_ids=writer_item_ids,
            selected_item_ids=selected_item_ids,
        )
        for item in pool_items
    ]
    writer_summaries = [item for item in summaries if item["in_writer_pool"]]
    excluded_items = [item for item in summaries if item["exclusion_reasons"]]
    selected_summaries = [item for item in summaries if item["selected"]]

    return {
        "workflow_mode": workflow_mode,
        "selection_mode": selection_mode,
        "decision": decision,
        "reason": reason,
        "message": message,
        "pool_count": len(pool_items),
        "writer_input_count": len(writer_items),
        "selected_count": len(selected_items),
        "high_signal_threshold": int(
            service.settings.briefing_quiet_day_high_signal_threshold
        ),
        "min_high_signal_to_publish": max(
            1, int(service.settings.briefing_min_high_signal_to_publish)
        ),
        "pool_high_signal_count": sum(1 for item in summaries if item["high_signal"]),
        "writer_high_signal_count": sum(
            1 for item in writer_summaries if item["high_signal"]
        ),
        "blocked_existing_briefing_count": sum(
            1 for item in summaries if item["blocked_by_existing_briefing"]
        ),
        "source_floor_reject_count": sum(
            1
            for item in writer_summaries
            if not item["source_floor_passed"] and not item["selected"]
        ),
        "recent_novelty_reject_count": sum(
            1
            for item in writer_summaries
            if item["duplicate_recent_briefing"] and not item["selected"]
        ),
        "selected_items": selected_summaries,
        "excluded_items": excluded_items,
        "pool_items": summaries,
    }


def select_items_for_workflow(
    service: Any,
    item_dicts: list[dict[str, Any]],
    workflow_mode: str,
    recent_published: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Select items for one workflow mode without mutating DB state."""
    if workflow_mode == REGULAR_MONTHLY_NEWSLETTER_MODE:
        selected = select_items_for_monthly_newsletter(service, item_dicts)
        if not selected:
            return {
                "decision": "skip",
                "reason": "not_enough_diverse_items_after_monthly_selection",
                "message": (
                    "Monthly newsletter skipped: not enough diverse high-signal "
                    "items after selection constraints."
                ),
                "mode": "monthly_newsletter",
                "selected_items": [],
            }
        return {
            "decision": "generate",
            "reason": "monthly_selection_ready",
            "message": "",
            "mode": "monthly_newsletter",
            "selected_items": selected,
        }

    if workflow_mode == WEEKLY_FLAGSHIP_MODE:
        selected = select_items_for_weekly_flagship(service, item_dicts)
        if not selected:
            return {
                "decision": "skip",
                "reason": "not_enough_diverse_items_after_weekly_selection",
                "message": (
                    "Weekly flagship skipped: not enough diverse high-signal "
                    "items from the last week."
                ),
                "mode": "weekly_flagship",
                "selected_items": [],
            }
        return {
            "decision": "generate",
            "reason": "weekly_selection_ready",
            "message": "",
            "mode": "weekly_flagship",
            "selected_items": selected,
        }

    if bool(service.settings.briefing_skip_if_no_high_signal):
        high_signal = service.selector.high_signal_count(item_dicts)
        min_high_signal = max(
            1, int(service.settings.briefing_min_high_signal_to_publish)
        )
        if high_signal < min_high_signal:
            return {
                "decision": "skip",
                "reason": f"high_signal_below_threshold:{high_signal}<{min_high_signal}",
                "message": (
                    "Quiet day — not enough high-signal items "
                    f"({high_signal} < {min_high_signal}). Skipping briefing."
                ),
                "mode": "standard",
                "selected_items": [],
            }

    quiet_mode = is_quiet_day(service, item_dicts)
    mode = "quiet_day" if quiet_mode else "standard"
    selected = select_items_for_briefing(
        service,
        item_dicts,
        recent_published=list(recent_published or []),
        quiet_mode=quiet_mode,
    )
    if not selected:
        return {
            "decision": "skip",
            "reason": "no_items_remained_after_selection_constraints",
            "message": "No items remained after selection constraints. Skipping briefing.",
            "mode": mode,
            "selected_items": [],
        }
    return {
        "decision": "generate",
        "reason": "selection_ready",
        "message": "",
        "mode": mode,
        "selected_items": selected,
    }


def select_items_for_briefing(
    service: Any,
    items: list[dict[str, Any]],
    recent_published: list[dict[str, Any]] | None = None,
    *,
    quiet_mode: bool = False,
) -> list[dict[str, Any]]:
    """Select daily briefing items with novelty and diversity constraints."""
    return service.selector.select_items(
        items=items,
        recent_published=recent_published,
        quiet_mode=quiet_mode,
    )


def priority_score(
    service: Any,
    item: dict[str, Any],
    recent_published: list[dict[str, Any]] | None = None,
) -> float:
    """Return the selector priority score for one item."""
    return service.selector.priority_score(item, recent_published)


def passes_source_floor(service: Any, item: dict[str, Any]) -> bool:
    """Return whether an item clears the selector's source floor."""
    return service.selector.passes_source_floor(item)


def is_quiet_day(service: Any, items: list[dict[str, Any]]) -> bool:
    """Return whether the pool should use quiet-day briefing mode."""
    return service.selector.is_quiet_day(items)


def select_items_for_monthly_newsletter(
    service: Any,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Select a broader, story-deduped set of items for monthly mode."""
    return _select_ranked_deduped(
        service,
        items,
        min_items=max(1, int(service.settings.monthly_newsletter_min_items)),
        max_items=int(service.settings.monthly_newsletter_max_items),
        per_domain_cap=max(
            1, int(service.settings.monthly_newsletter_max_items_per_domain)
        ),
    )


def select_items_for_weekly_flagship(
    service: Any,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Select the week's strongest story-deduped items for the flagship."""
    return _select_ranked_deduped(
        service,
        items,
        min_items=max(1, int(service.settings.weekly_flagship_min_items)),
        max_items=int(service.settings.weekly_flagship_max_items),
        per_domain_cap=max(
            1, int(service.settings.monthly_newsletter_max_items_per_domain)
        ),
    )


def _select_ranked_deduped(
    service: Any,
    items: list[dict[str, Any]],
    *,
    min_items: int,
    max_items: int,
    per_domain_cap: int,
) -> list[dict[str, Any]]:
    max_items = max(min_items, int(max_items))

    ranked = sorted(
        items,
        key=lambda item: (
            int(item.get("relevance_score", 0) or 0),
            priority_score(service, item),
        ),
        reverse=True,
    )
    selected: list[dict[str, Any]] = []
    domain_counts: dict[str, int] = {}
    for item in ranked:
        if service.selector.is_duplicate_of(item, selected):
            continue
        url = str(item.get("url", "") or "")
        domain = (urlparse(url).netloc or "").strip().lower()
        if domain and domain_counts.get(domain, 0) >= per_domain_cap:
            continue
        selected.append(item)
        if domain:
            domain_counts[domain] = domain_counts.get(domain, 0) + 1
        if len(selected) >= max_items:
            break
    return selected if len(selected) >= min_items else []


def char_limits(
    service: Any,
    mode: str,
    *,
    selected_count: int | None = None,
) -> tuple[int, int, int]:
    """Return min/target/max body lengths for a given mode."""
    min_chars, target_chars, hard_max_chars = service.quality.char_limits(mode)
    if selected_count is not None and selected_count <= 1:
        min_chars = min(
            min_chars,
            int(service.settings.briefing_single_item_min_chars),
        )
        target_chars = min(
            target_chars,
            int(service.settings.briefing_single_item_target_chars),
        )
        hard_max_chars = min(
            hard_max_chars,
            int(service.settings.briefing_single_item_hard_max_chars),
        )
        target_chars = max(min_chars, target_chars)
        hard_max_chars = max(target_chars, hard_max_chars)
    return min_chars, target_chars, hard_max_chars


__all__ = [
    "build_selection_trace",
    "char_limits",
    "is_quiet_day",
    "passes_source_floor",
    "priority_score",
    "select_items_for_briefing",
    "select_items_for_monthly_newsletter",
    "select_items_for_workflow",
]
