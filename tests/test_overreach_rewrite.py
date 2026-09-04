"""Verifier overreach findings trigger a guarded scoping rewrite."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bcn.services.writer.drafting import _scoping_rewrite
from bcn.services.writer.review import extract_sticky_rewrite_constraints
from bcn.services.writer.review import verifier_overreach_issues


def test_verifier_overreach_issues_matches_real_verifier_language():
    verifier = {
        "passed": True,
        "score": 88,
        "issues": [
            "The SiYuan wording \"one ' in the keyword, stacked SQL runs\" overstates exploitability.",
            "NetStaX's \"silent foothold\" implies successful code execution, while the summary supports memory corruption.",
            "The unstructured affected range is more specific than the summary.",
            "Grammar: 'gives' should be 'give'.",
            "The version is listed as unsupported upstream.",
        ],
    }
    flagged = verifier_overreach_issues(verifier)
    assert len(flagged) == 3
    assert all("Grammar" not in issue and "listed as" not in issue for issue in flagged)


def test_verifier_overreach_issues_empty_or_malformed():
    assert verifier_overreach_issues({"issues": []}) == []
    assert verifier_overreach_issues({"issues": "not a list"}) == []
    assert verifier_overreach_issues({}) == []


def test_sticky_constraint_added_for_overstatement():
    constraints = extract_sticky_rewrite_constraints(
        {"issues": []},
        {"issues": ["'silent foothold' overstates what the source establishes."]},
    )
    assert any("Scope every exploitability claim" in c for c in constraints)


def _evaluation(markdown: str, overreach: list[str], critic: int = 90) -> dict:
    return {
        "markdown": markdown,
        "release_passed": True,
        "gate": {"passed": True, "hard_issues": []},
        "critique": {"score": critic, "issues": []},
        "verifier": {"passed": True, "score": 88, "issues": overreach},
        "min_chars": 1200,
        "target_chars": 1700,
        "hard_max_chars": 2300,
        "rewrites": 0,
    }


def _service(*, postprocessed_items: int, revised_eval: dict, revise_raises: bool = False):
    items = [{"id": "a", "url": "https://a"}, {"id": "b", "url": "https://b"}]
    revise = AsyncMock(side_effect=RuntimeError("timeout")) if revise_raises else AsyncMock(
        return_value="scoped draft"
    )
    return (
        SimpleNamespace(
            settings=SimpleNamespace(),
            writer_llm=SimpleNamespace(revise_briefing=revise),
            build_rewrite_feedback_context=lambda **kwargs: {"ctx": True},
            build_preference_rationale=lambda feedback: "scoped",
            postprocess_briefing=AsyncMock(
                return_value=SimpleNamespace(
                    markdown="scoped draft", selected_items=items[:postprocessed_items]
                )
            ),
            evaluate_existing_markdown=AsyncMock(return_value=revised_eval),
        ),
        items,
    )


@pytest.mark.asyncio
async def test_scoping_rewrite_adopts_improved_draft():
    original = _evaluation("sharp but overstated", ["'foothold' overstates the source."])
    improved = _evaluation("scoped draft", [], critic=91)
    service, items = _service(postprocessed_items=2, revised_eval=improved)
    trace: list = []
    pairs: list = []

    result = await _scoping_rewrite(
        service, evaluation=original, selected_items=items, history=[], mode="standard",
        sticky_constraints=[], max_rewrites=7, budget=1, trace_rounds=trace, preference_pairs=pairs,
    )

    assert result["markdown"] == "scoped draft"
    assert result["rewrites"] == 1
    assert trace[-1]["phase"] == "scoping_rewrite"
    assert pairs and pairs[0]["source"] == "scoping_rewrite"
    assert pairs[0]["rejected_text"] == "sharp but overstated"


@pytest.mark.asyncio
async def test_scoping_rewrite_rejects_coverage_drop():
    original = _evaluation("sharp but overstated", ["'foothold' overstates the source."])
    improved = _evaluation("scoped draft", [])
    service, items = _service(postprocessed_items=1, revised_eval=improved)
    trace: list = []
    pairs: list = []

    result = await _scoping_rewrite(
        service, evaluation=original, selected_items=items, history=[], mode="standard",
        sticky_constraints=[], max_rewrites=7, budget=1, trace_rounds=trace, preference_pairs=pairs,
    )

    assert result is original
    assert result["markdown"] == "sharp but overstated"
    assert trace[-1]["phase"] == "scoping_rewrite_failed"
    assert pairs == []


@pytest.mark.asyncio
async def test_scoping_rewrite_keeps_original_on_exception_or_regression():
    original = _evaluation("sharp but overstated", ["'foothold' overstates the source."])
    service, items = _service(postprocessed_items=2, revised_eval={}, revise_raises=True)
    trace: list = []
    pairs: list = []
    result = await _scoping_rewrite(
        service, evaluation=original, selected_items=items, history=[], mode="standard",
        sticky_constraints=[], max_rewrites=7, budget=1, trace_rounds=trace, preference_pairs=pairs,
    )
    assert result is original
    assert trace[-1]["phase"] == "scoping_rewrite_failed"

    # A rewrite that hedges away the critic score is rejected even if it passes.
    duller = _evaluation("hedged draft", [], critic=80)
    service, items = _service(postprocessed_items=2, revised_eval=duller)
    trace = []
    result = await _scoping_rewrite(
        service, evaluation=original, selected_items=items, history=[], mode="standard",
        sticky_constraints=[], max_rewrites=7, budget=1, trace_rounds=trace, preference_pairs=pairs,
    )
    assert result is original
    assert trace[-1]["phase"] == "scoping_rewrite_rejected"
    assert result["rewrites"] == 1
    assert pairs == []


@pytest.mark.asyncio
async def test_scoping_rewrite_noop_without_overreach_or_budget():
    clean = _evaluation("clean draft", [])
    service, items = _service(postprocessed_items=2, revised_eval=clean)
    assert (
        await _scoping_rewrite(
            service, evaluation=clean, selected_items=items, history=[], mode="standard",
            sticky_constraints=[], max_rewrites=7, budget=1, trace_rounds=[], preference_pairs=[],
        )
        is clean
    )
    overstated = _evaluation("x", ["overstates"])
    assert (
        await _scoping_rewrite(
            service, evaluation=overstated, selected_items=items, history=[], mode="standard",
            sticky_constraints=[], max_rewrites=7, budget=0, trace_rounds=[], preference_pairs=[],
        )
        is overstated
    )
    service.writer_llm.revise_briefing.assert_not_awaited()
