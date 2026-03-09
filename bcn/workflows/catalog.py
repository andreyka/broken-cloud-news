"""Declarative catalog of scheduled BCN workflows."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from bcn.common.config import Settings
from bcn.workflows.automation import build_regular_briefing_trigger
from bcn.workflows.automation import build_regular_monthly_newsletter_trigger
from bcn.workflows.automation import build_shadow_regular_briefing_trigger
from bcn.workflows.automation import execute_scheduled_analysis
from bcn.workflows.automation import execute_scheduled_collection
from bcn.workflows.automation import execute_shadow_regular_briefing
from bcn.workflows.modes import REGULAR_DAILY_BRIEFING_MODE
from bcn.workflows.modes import REGULAR_MONTHLY_NEWSLETTER_MODE
from bcn.workflows.modes.common import run_generation_and_distribution
from bcn.workflows.runtime import WorkflowRuntime

TriggerBuilder = Callable[[Settings], object]
EnabledPredicate = Callable[[Settings], bool]
WorkflowExecutionKind = Literal["collect", "analyze", "shadow", "publish_pipeline"]


@dataclass(frozen=True)
class WorkflowStepDefinition:
    """One logical step within a scheduled workflow."""

    step_id: str
    component: str
    operation: str


@dataclass(frozen=True)
class ScheduledWorkflowDefinition:
    """Declarative scheduled workflow definition used by the control plane."""

    workflow_id: str
    description: str
    steps: tuple[WorkflowStepDefinition, ...]
    build_trigger: TriggerBuilder
    execution_kind: WorkflowExecutionKind
    collection_source: str = ""
    workflow_mode: str = ""
    enabled_when: EnabledPredicate = lambda _settings: True

    def is_enabled(self, settings: Settings) -> bool:
        """Return whether this workflow should be registered."""
        return bool(self.enabled_when(settings))

    async def execute(self, runtime: WorkflowRuntime) -> None:
        """Execute this scheduled workflow from typed metadata."""
        if self.execution_kind == "collect":
            if not self.collection_source:
                raise ValueError(
                    f"Workflow {self.workflow_id} is missing collection_source"
                )
            await execute_scheduled_collection(
                runtime,
                source=self.collection_source,
            )
            return

        if self.execution_kind == "analyze":
            await execute_scheduled_analysis(runtime)
            return

        if self.execution_kind == "shadow":
            await execute_shadow_regular_briefing(runtime)
            return

        if self.execution_kind == "publish_pipeline":
            if not self.workflow_mode:
                raise ValueError(
                    f"Workflow {self.workflow_id} is missing workflow_mode"
                )
            await run_generation_and_distribution(
                runtime=runtime,
                mode=self.workflow_mode,
            )
            return

        raise ValueError(
            f"Unsupported workflow execution kind for {self.workflow_id}: "
            f"{self.execution_kind}"
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
        steps=(WorkflowStepDefinition("collect_ghsa", "collector", "collect"),),
        build_trigger=_interval_hours("ghsa_interval_hours"),
        execution_kind="collect",
        collection_source="ghsa",
    ),
    ScheduledWorkflowDefinition(
        workflow_id="rss_collector",
        description="Collect RSS items from configured feeds.",
        steps=(WorkflowStepDefinition("collect_rss", "collector", "collect"),),
        build_trigger=_interval_hours("rss_interval_hours"),
        execution_kind="collect",
        collection_source="rss",
    ),
    ScheduledWorkflowDefinition(
        workflow_id="reddit_collector",
        description="Collect Reddit items from configured subreddits.",
        steps=(WorkflowStepDefinition("collect_reddit", "collector", "collect"),),
        build_trigger=_interval_hours("reddit_interval_hours"),
        execution_kind="collect",
        collection_source="reddit",
    ),
    ScheduledWorkflowDefinition(
        workflow_id="twitter_collector",
        description="Collect Twitter/X items from configured handles.",
        steps=(WorkflowStepDefinition("collect_twitter", "collector", "collect"),),
        build_trigger=_interval_hours("twitter_interval_hours"),
        execution_kind="collect",
        collection_source="twitter",
    ),
    ScheduledWorkflowDefinition(
        workflow_id="analyst",
        description="Analyze newly collected items.",
        steps=(WorkflowStepDefinition("analyze_pending", "analyst", "analyze_item"),),
        build_trigger=_interval_minutes("analyst_interval_minutes"),
        execution_kind="analyze",
    ),
    ScheduledWorkflowDefinition(
        workflow_id=f"{REGULAR_DAILY_BRIEFING_MODE}_shadow",
        description="Run the pre-publish shadow evaluation lane.",
        steps=(WorkflowStepDefinition("shadow_compare", "workflow", "shadow_lane"),),
        build_trigger=build_shadow_regular_briefing_trigger,
        execution_kind="shadow",
        enabled_when=lambda settings: bool(settings.shadow_enabled),
    ),
    ScheduledWorkflowDefinition(
        workflow_id=REGULAR_DAILY_BRIEFING_MODE,
        description="Run the regular daily briefing publish pipeline.",
        steps=(
            WorkflowStepDefinition("generate_briefing", "writer", "generate_release_candidate"),
            WorkflowStepDefinition("distribute_briefing", "distributor", "deliver"),
        ),
        build_trigger=build_regular_briefing_trigger,
        execution_kind="publish_pipeline",
        workflow_mode=REGULAR_DAILY_BRIEFING_MODE,
    ),
    ScheduledWorkflowDefinition(
        workflow_id=REGULAR_MONTHLY_NEWSLETTER_MODE,
        description="Run the regular monthly newsletter publish pipeline.",
        steps=(
            WorkflowStepDefinition("generate_newsletter", "writer", "generate_release_candidate"),
            WorkflowStepDefinition("distribute_newsletter", "distributor", "deliver"),
        ),
        build_trigger=build_regular_monthly_newsletter_trigger,
        execution_kind="publish_pipeline",
        workflow_mode=REGULAR_MONTHLY_NEWSLETTER_MODE,
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
