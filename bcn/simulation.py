"""Compatibility exports for simulation utilities.

The implementation now lives under ``bcn.evaluation.simulation`` so the
evaluation package owns all replay/benchmark/shadow logic.
"""

from bcn.evaluation.simulation import _build_decision_summary
from bcn.evaluation.simulation import compare_simulation_reports
from bcn.evaluation.simulation import score_feedback_rubric
from bcn.evaluation.simulation import simulate_historical_briefings

__all__ = [
    "_build_decision_summary",
    "compare_simulation_reports",
    "score_feedback_rubric",
    "simulate_historical_briefings",
]
