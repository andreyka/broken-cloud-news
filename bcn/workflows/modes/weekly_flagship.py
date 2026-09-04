"""Weekly flagship edition: generated on schedule, held for human review."""

from __future__ import annotations

import logging

from bcn.common.config import Settings
from bcn.contracts.modes import WEEKLY_FLAGSHIP_MODE
from bcn.contracts.workflow import WriterHandoffResult
from bcn.workflows.modes._schedule import schedule_start_time
from bcn.workflows.runtime import WorkflowRuntime

logger = logging.getLogger(__name__)

MODE = WEEKLY_FLAGSHIP_MODE

AWAITING_REVIEW_STATUS = "AWAITING_REVIEW"


def build_trigger(settings: Settings):
    """Build the cron trigger for the weekly flagship generation."""
    from apscheduler.triggers.cron import CronTrigger

    return CronTrigger(
        day_of_week=str(settings.weekly_flagship_day_of_week or "thu")
        .strip()
        .lower(),
        hour=int(settings.weekly_flagship_hour),
        minute=int(settings.weekly_flagship_minute),
        timezone=settings.weekly_flagship_timezone,
        start_time=schedule_start_time(settings.weekly_flagship_timezone),
    )


async def hold_for_review(
    settings: Settings,
    writer_result: WriterHandoffResult | str,
) -> None:
    """Park a generated flagship draft for human sign-off instead of publishing."""
    from bcn.common.alerts import send_operator_alert
    from bcn.persistence.briefings import set_briefing_status

    handoff = (
        writer_result.handoff
        if isinstance(writer_result, WriterHandoffResult)
        else None
    )
    if handoff is None or handoff.decision != "publish" or handoff.briefing_id is None:
        logger.info("Weekly flagship produced no publishable draft; nothing to hold.")
        return
    await set_briefing_status(handoff.briefing_id, AWAITING_REVIEW_STATUS)
    logger.info(
        "Weekly flagship draft %s held for review; approve with "
        "`bcn flagship-approve`.",
        handoff.briefing_id,
    )
    await send_operator_alert(
        settings,
        "Weekly flagship draft is ready for your review and signature. "
        f"Approve with: bcn flagship-approve  (briefing {handoff.briefing_id})",
    )


async def run(runtime: WorkflowRuntime) -> None:
    """Generate one weekly flagship draft and hold it for review."""
    from bcn.workflows.generation import execute_generation_result

    writer_result = await execute_generation_result(
        runtime.settings,
        mode=MODE,
        source="scheduler",
        manage_pool=False,
    )
    await hold_for_review(runtime.settings, writer_result)
