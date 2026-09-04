"""Control-plane distribution service for briefing delivery."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import logging
from uuid import UUID

from bcn.common.config import Settings
from bcn.contracts.distributor import DeliveryRequest
from bcn.contracts.distributor import REGULAR_MONTHLY_NEWSLETTER_MODE
from bcn.contracts.modes import WEEKLY_FLAGSHIP_MODE
from bcn.contracts.distributor import normalize_distribution_mode
from bcn.contracts.services import DistributorWorkflow
from bcn.persistence.briefings import claim_draft_briefing_by_id
from bcn.persistence.briefings import claim_latest_draft_briefing
from bcn.persistence.briefings import mark_briefing_distributed
from bcn.persistence.briefings import release_briefing_for_retry
from bcn.persistence.news_items import mark_items_published
from bcn.persistence.newsletter import get_newsletter_subscribers
from bcn.persistence.runtime import close_pool
from bcn.persistence.runtime import get_pool
from bcn.persistence.training import get_distribution_outcomes
from bcn.persistence.training import upsert_distribution_outcome
logger = logging.getLogger(__name__)


async def _claim_briefing(
    settings: Settings,
    *,
    briefing_id: UUID | None,
):
    """Claim the requested or latest draft briefing for distribution."""
    claim_kwargs = {
        "stale_distributing_minutes": (
            settings.distribution_retry_stale_distributing_minutes
        ),
        "max_distribution_retries": settings.distribution_retry_max_attempts,
    }
    if briefing_id is not None:
        return await claim_draft_briefing_by_id(briefing_id, **claim_kwargs)
    return await claim_latest_draft_briefing(**claim_kwargs)


def _previously_ok_channels(rows: list[object]) -> set[str]:
    """Extract channels that already have a successful delivery outcome."""
    channels: set[str] = set()
    for row in rows:
        try:
            channel = str(row["channel"]).strip().lower()
            status = str(row["status"] or "").strip().lower()
        except Exception:
            continue
        if channel and status == "ok":
            channels.add(channel)
    return channels


def _stale_draft_message(
    briefing: dict[str, object],
    *,
    max_age_minutes: int,
) -> str | None:
    """Return a stale-draft message when the claimed briefing is too old."""
    if max_age_minutes <= 0:
        return None
    created_at = briefing.get("created_at")
    if not isinstance(created_at, datetime):
        return None
    created_utc = (
        created_at.astimezone(timezone.utc)
        if created_at.tzinfo is not None
        else created_at.replace(tzinfo=timezone.utc)
    )
    age = datetime.now(timezone.utc) - created_utc
    if age <= timedelta(minutes=max_age_minutes):
        return None
    age_minutes = int(age.total_seconds() // 60)
    return f"Latest draft is stale ({age_minutes} minutes old), skipping distribution"


async def _newsletter_recipients_for_mode(mode: str) -> list[str]:
    """Load newsletter subscribers for the monthly and weekly editions."""
    if mode not in (REGULAR_MONTHLY_NEWSLETTER_MODE, WEEKLY_FLAGSHIP_MODE):
        return []
    rows = await get_newsletter_subscribers(active_only=True)
    recipients: list[str] = []
    for row in rows:
        email = str(dict(row).get("email") or "").strip()
        if email:
            recipients.append(email)
    return recipients


async def execute_distribution(
    settings: Settings,
    *,
    mode: str,
    briefing_id: UUID | None = None,
    distributor_service: DistributorWorkflow | None = None,
    manage_pool: bool = True,
) -> str:
    """Claim, deliver, and persist one distribution attempt."""
    await get_pool(settings)
    if distributor_service is None:
        from bcn.service_registry import build_distributor_workflow

        active_service = build_distributor_workflow(settings)
    else:
        active_service = distributor_service
    owns_service = distributor_service is None
    claimed_briefing = None

    try:
        claimed_briefing = await _claim_briefing(settings, briefing_id=briefing_id)
        if claimed_briefing is None:
            if briefing_id is not None:
                return (
                    f"Requested briefing {briefing_id} is not available for distribution"
                )
            return "No new briefing to distribute"

        should_release_for_retry = True
        retry_error: str | None = None
        try:
            normalized_mode = normalize_distribution_mode(mode)
            # A reviewed flagship is fresh by definition when approved; the
            # stale guard exists for unattended daily drafts only.
            if normalized_mode != WEEKLY_FLAGSHIP_MODE:
                stale_message = _stale_draft_message(
                    dict(claimed_briefing),
                    max_age_minutes=max(
                        0,
                        int(settings.briefing_distribution_max_draft_age_minutes),
                    ),
                )
                if stale_message:
                    return stale_message

            newsletter_recipients = await _newsletter_recipients_for_mode(
                normalized_mode
            )
            previous_rows = await get_distribution_outcomes(
                briefing_ids=[claimed_briefing["id"]]
            )
            delivery = await active_service.deliver(
                DeliveryRequest(
                    briefing=dict(claimed_briefing),
                    mode=normalized_mode,
                    newsletter_recipients=tuple(newsletter_recipients),
                    previous_ok_channels=frozenset(
                        _previously_ok_channels(previous_rows)
                    ),
                )
            )

            for attempt in delivery.attempts:
                await upsert_distribution_outcome(
                    briefing_id=claimed_briefing["id"],
                    channel=attempt.channel,
                    status=attempt.status,
                    external_message_id=attempt.external_message_id,
                    metrics={},
                    metadata=attempt.metadata,
                )

            if delivery.all_ok:
                await mark_briefing_distributed(
                    claimed_briefing["id"],
                    delivery.results,
                )
                item_ids = (
                    list(claimed_briefing["item_ids"])
                    if claimed_briefing["item_ids"]
                    else []
                )
                await mark_items_published(item_ids)
                if settings.ai_review_auto_enabled:
                    try:
                        from bcn.workflows.ai_review import get_ai_review_config
                        from bcn.workflows.queue import enqueue_ai_review_job

                        if get_ai_review_config(settings).enabled:
                            await enqueue_ai_review_job(
                                settings,
                                briefing_id=claimed_briefing["id"],
                                source="auto_distribution",
                                notes="Queued after successful distribution.",
                            )
                    except Exception:
                        logger.exception(
                            "Failed to enqueue automatic AI review for briefing %s",
                            claimed_briefing["id"],
                        )
                should_release_for_retry = False
            else:
                failed_channels = sorted(
                    channel
                    for channel, status in delivery.results.items()
                    if status != "ok"
                )
                if failed_channels:
                    retry_error = "distribution_incomplete:" + ",".join(
                        failed_channels
                    )
            logger.info(delivery.message)
            return delivery.message
        finally:
            if should_release_for_retry and claimed_briefing is not None:
                await release_briefing_for_retry(
                    claimed_briefing["id"],
                    error=retry_error,
                    max_retries=settings.distribution_retry_max_attempts,
                    base_delay_seconds=settings.distribution_retry_base_delay_seconds,
                    max_delay_seconds=settings.distribution_retry_max_delay_seconds,
                )
    finally:
        if owns_service:
            await active_service.close()
        if manage_pool:
            await close_pool()
