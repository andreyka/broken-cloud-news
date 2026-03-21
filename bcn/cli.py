"""CLI entry-point and daemon mode for Broken Cloud News."""

from __future__ import annotations

import logging
import sys

import click

from bcn.cli_commands.core import register_core_commands
from bcn.cli_commands.distribution import register_distribution_commands
from bcn.cli_commands.evaluation import register_evaluation_commands
from bcn.cli_commands.history import register_history_commands
from bcn.cli_commands.newsletter import register_newsletter_commands
from bcn.cli_commands.optimization import register_optimization_commands
from bcn.cli_commands.recovery import register_recovery_commands
from bcn.cli_commands.review import register_review_commands
from bcn.cli_commands.training import register_training_commands
from bcn.transports.http.server import serve_component_http
from bcn.workflows.automation import build_regular_briefing_trigger
from bcn.workflows.automation import build_regular_monthly_newsletter_trigger
from bcn.workflows.modes import ALL_MODES
from bcn.workflows.analysis import execute_analysis
from bcn.workflows.collection import execute_collection
from bcn.workflows.distribution import execute_distribution
from bcn.workflows.generation import execute_generation as execute_briefing_generation
from bcn.workflows.review import execute_critique as critique_briefing
from bcn.workflows.review import execute_verification as verify_briefing
from bcn.workflows.service import execute_workflow_mode
from bcn.workflows.service import run_daemon
from bcn.workflows.service import run_scheduler
from bcn.workflows.service import run_worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

_WORKFLOW_MODE_CHOICES = click.Choice(list(ALL_MODES), case_sensitive=True)


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
def cli(verbose: bool) -> None:
    """Broken Cloud News cloud security briefing services."""
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

register_core_commands(cli, _WORKFLOW_MODE_CHOICES, bindings=sys.modules[__name__])
register_evaluation_commands(cli, _WORKFLOW_MODE_CHOICES)
register_newsletter_commands(cli)
register_optimization_commands(cli)
register_review_commands(cli)
register_distribution_commands(cli)
register_history_commands(cli)
register_training_commands(cli)
register_recovery_commands(cli)


__all__ = [
    "_WORKFLOW_MODE_CHOICES",
    "cli",
    "critique_briefing",
    "execute_analysis",
    "execute_briefing_generation",
    "execute_collection",
    "execute_distribution",
    "execute_workflow_mode",
    "run_daemon",
    "run_scheduler",
    "run_worker",
    "serve_component_http",
    "verify_briefing",
]
