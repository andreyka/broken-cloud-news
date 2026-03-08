"""Shared runtime dependencies for workflow execution."""

from __future__ import annotations

from dataclasses import dataclass

from bcn.common.config import Settings


@dataclass(frozen=True)
class WorkflowRuntime:
    """Explicit runtime dependencies shared across scheduler workflow jobs."""

    settings: Settings


def build_workflow_runtime(settings: Settings) -> WorkflowRuntime:
    """Build an explicit workflow runtime for scheduler and mode execution."""
    return WorkflowRuntime(settings=settings)
