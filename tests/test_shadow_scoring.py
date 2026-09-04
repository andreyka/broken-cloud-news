"""Shadow-lane scoring: blended verdicts and imperative-vocabulary actionability."""

from bcn.evaluation.lanes import _blended_score
from bcn.evaluation.lanes import build_shadow_summary
from bcn.evaluation.simulation import _ACTIONABLE_TERMS


def test_blended_score_averages_available_judges():
    assert _blended_score(90, 89, 96) == 92
    assert _blended_score(86, 95, 88) == 90
    # Missing judges (0) are ignored rather than dragging the mean down.
    assert _blended_score(82, 0, 0) == 82
    assert _blended_score(0, 0, 0) == 0


def test_shadow_summary_uses_critic_and_verifier_not_rubric_alone():
    def arm(rubric: int, critic: int, verifier: int) -> dict:
        return {
            "decision": "generate",
            "release_passed": True,
            "rubric": {"score": rubric},
            "critique": {"score": critic},
            "verifier": {"score": verifier},
            "gate": {"hard_issues": []},
        }

    # The 2026-09-04 case: rubric favored the champion on vocabulary alone
    # while the critic strongly preferred the candidate.
    report = {
        "champion": arm(90, 89, 96),
        "candidate": arm(86, 95, 88),
        "selection_overlap_ratio": 1.0,
    }
    summary = build_shadow_summary(report)
    assert summary["champion_score"] == 92
    assert summary["candidate_score"] == 90
    assert summary["recommendation"] == "hold"
    assert summary["components"]["candidate"]["critic"] == 95


def test_actionable_terms_include_practitioner_imperatives():
    for term in ("bump", "ship", "isolate", "hunt", "revoke"):
        assert term in _ACTIONABLE_TERMS
