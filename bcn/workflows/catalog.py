"""Declarative catalog of scheduled BCN workflows."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from bcn.common.config import Settings
from bcn.contracts.modes import REGULAR_DAILY_BRIEFING_MODE
from bcn.contracts.modes import REGULAR_MONTHLY_NEWSLETTER_MODE
from bcn.workflows.automation import build_regular_briefing_trigger
from bcn.workflows.automation import build_regular_monthly_newsletter_trigger
from bcn.workflows.automation import build_shadow_regular_briefing_trigger
from bcn.workflows.runtime import WorkflowRuntime

TriggerBuilder = Callable[[Settings], object]
EnabledPredicate = Callable[[Settings], bool]


@dataclass(frozen=True)
class WorkflowStepDefinition:
    """One logical step within a scheduled workflow."""

    step_id: str
    component: str
    operation: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScheduledWorkflowDefinition:
    """Declarative scheduled workflow definition used by the control plane."""

    workflow_id: str
    description: str
    steps: tuple[WorkflowStepDefinition, ...]
    build_trigger: TriggerBuilder
    enabled_when: EnabledPredicate = lambda _settings: True

    def is_enabled(self, settings: Settings) -> bool:
        """Return whether this workflow should be registered."""
        return bool(self.enabled_when(settings))

    async def execute(self, runtime: WorkflowRuntime) -> None:
        """Execute this scheduled workflow by dispatching its declared steps."""
        from bcn.workflows.execution import execute_workflow_steps

        await execute_workflow_steps(
            runtime,
            workflow_id=self.workflow_id,
            steps=self.steps,
        )


def _interval_hours(settings_field: str) -> TriggerBuilder:
    def _build(settings: Settings):
        from apscheduler.triggers.interval import IntervalTrigger

        return IntervalTrigger(hours=int(getattr(settings, settings_field)))

    return _build


def _interval_minutes(settings_field: str) -> TriggerBuilder:
    def _build(settings: Settings):
        from apscheduler.triggers.interval import IntervalTrigger

        return IntervalTrigger(minutes=int(getattr(settings, settings_field)))

    return _build


_CATALOG: tuple[ScheduledWorkflowDefinition, ...] = (
    ScheduledWorkflowDefinition(
        workflow_id="ghsa_collector",
        description="Collect GitHub Security Advisory items.",
        steps=(
            WorkflowStepDefinition(
                "collect_ghsa",
                "collector",
                "collect",
                args={"source": "ghsa"},
            ),
        ),
        build_trigger=_interval_hours("ghsa_interval_hours"),
    ),
    ScheduledWorkflowDefinition(
        workflow_id="rss_collector",
        description="Collect RSS items from configured feeds.",
        steps=(
            WorkflowStepDefinition(
                "collect_rss",
                "collector",
                "collect",
                args={"source": "rss"},
            ),
        ),
        build_trigger=_interval_hours("rss_interval_hours"),
    ),
    ScheduledWorkflowDefinition(
        workflow_id="reddit_collector",
        description="Collect Reddit items from configured subreddits.",
        steps=(
            WorkflowStepDefinition(
                "collect_reddit",
                "collector",
                "collect",
                args={"source": "reddit"},
            ),
        ),
        build_trigger=_interval_hours("reddit_interval_hours"),
    ),
    ScheduledWorkflowDefinition(
        workflow_id="twitter_collector",
        description="Collect Twitter/X items from configured handles.",
        steps=(
            WorkflowStepDefinition(
                "collect_twitter",
                "collector",
                "collect",
                args={"source": "twitter"},
            ),
        ),
        build_trigger=_interval_hours("twitter_interval_hours"),
    ),
    ScheduledWorkflowDefinition(
        workflow_id="analyst",
        description="Analyze newly collected items.",
        steps=(WorkflowStepDefinition("analyze_pending", "analyst", "analyze_pending"),),
        build_trigger=_interval_minutes("analyst_interval_minutes"),
    ),
    ScheduledWorkflowDefinition(
        workflow_id=f"{REGULAR_DAILY_BRIEFING_MODE}_shadow",
        description="Run the pre-publish shadow evaluation lane.",
        steps=(
            WorkflowStepDefinition(
                "shadow_compare",
                "workflow",
                "shadow_regular_briefing",
            ),
        ),
        build_trigger=build_shadow_regular_briefing_trigger,
        enabled_when=lambda settings: bool(settings.shadow_enabled),
    ),
    ScheduledWorkflowDefinition(
        workflow_id=REGULAR_DAILY_BRIEFING_MODE,
        description="Run the regular daily briefing publish pipeline.",
        steps=(
            WorkflowStepDefinition(
                "generate_briefing",
                "writer",
                "generate_release_candidate",
                args={"mode": REGULAR_DAILY_BRIEFING_MODE},
            ),
            WorkflowStepDefinition(
                "distribute_briefing",
                "distributor",
                "deliver",
                args={"mode": REGULAR_DAILY_BRIEFING_MODE},
            ),
        ),
        build_trigger=build_regular_briefing_trigger,
    ),
    ScheduledWorkflowDefinition(
        workflow_id=REGULAR_MONTHLY_NEWSLETTER_MODE,
        description="Run the regular monthly newsletter publish pipeline.",
        steps=(
            WorkflowStepDefinition(
                "generate_newsletter",
                "writer",
                "generate_release_candidate",
                args={"mode": REGULAR_MONTHLY_NEWSLETTER_MODE},
            ),
            WorkflowStepDefinition(
                "distribute_newsletter",
                "distributor",
                "deliver",
                args={"mode": REGULAR_MONTHLY_NEWSLETTER_MODE},
            ),
        ),
        build_trigger=build_regular_monthly_newsletter_trigger,
        enabled_when=lambda settings: bool(settings.monthly_newsletter_enabled),
    ),
)


def iter_scheduled_workflows(
    settings: Settings,
) -> tuple[ScheduledWorkflowDefinition, ...]:
    """Return the enabled scheduled workflows for the current settings."""
    return tuple(definition for definition in _CATALOG if definition.is_enabled(settings))


__all__ = [
    "ScheduledWorkflowDefinition",
    "WorkflowStepDefinition",
    "iter_scheduled_workflows",
]
