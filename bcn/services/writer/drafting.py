"""Draft-generation and rewrite-loop helpers for writer workflows."""

from __future__ import annotations

from typing import Any


async def generate_release_candidate(
    service: Any,
    *,
    selected_items: list[dict[str, Any]],
    history: list[dict[str, Any]],
    mode: str,
) -> dict[str, Any]:
    """Generate and evaluate one release candidate without publishing it."""
    min_chars, target_chars, hard_max_chars = service.char_limits(
        mode,
        selected_count=len(selected_items),
    )
    draft = await service.writer_llm.generate_briefing(
        selected_items,
        recent_briefings=history,
        mode=mode,
    )
    active_selected_items = list(selected_items)
    postprocessed = await service.postprocess_briefing(
        briefing_body=draft,
        selected_items=active_selected_items,
        mode=mode,
        min_chars=min_chars,
        target_chars=target_chars,
        hard_max_chars=hard_max_chars,
    )
    draft = postprocessed.markdown
    active_selected_items = postprocessed.selected_items

    rewrites = 0
    max_rewrites = max(0, int(service.settings.briefing_critique_max_rounds))
    trace_rounds: list[dict[str, Any]] = []
    preference_pairs: list[dict[str, Any]] = []
    while True:
        evaluation = await service.evaluate_existing_markdown(
            markdown=draft,
            selected_items=active_selected_items,
            history=history,
            mode=mode,
        )
        round_input = str(evaluation["markdown"] or "")
        evaluation["rewrites"] = rewrites
        evaluation["selected_items"] = list(active_selected_items)
        if bool(evaluation["release_passed"]):
            trace_rounds.append(
                {
                    "round_index": len(trace_rounds),
                    "phase": "initial" if not trace_rounds else "rewrite",
                    "draft_input": round_input,
                    "gate_result": dict(evaluation["gate"]),
                    "critique_result": dict(evaluation["critique"]),
                    "verifier_result": dict(evaluation["verifier"]),
                    "feedback": [],
                    "rewrite_output": None,
                    "passed": True,
                }
            )
            evaluation["rounds"] = trace_rounds
            evaluation["preference_pairs"] = preference_pairs
            return evaluation
        if rewrites >= max_rewrites:
            trace_rounds.append(
                {
                    "round_index": len(trace_rounds),
                    "phase": "initial" if not trace_rounds else "rewrite",
                    "draft_input": round_input,
                    "gate_result": dict(evaluation["gate"]),
                    "critique_result": dict(evaluation["critique"]),
                    "verifier_result": dict(evaluation["verifier"]),
                    "feedback": [],
                    "rewrite_output": None,
                    "passed": False,
                }
            )
            evaluation["rounds"] = trace_rounds
            evaluation["preference_pairs"] = preference_pairs
            return evaluation

        gate = evaluation["gate"]
        critique = evaluation["critique"]
        verifier = evaluation["verifier"]
        feedback: list[str] = []
        feedback.extend(str(issue) for issue in gate.get("issues", []))
        feedback.extend(str(issue) for issue in critique.get("issues", []))
        feedback.extend(str(issue) for issue in critique.get("recommendations", []))
        feedback.extend(str(issue) for issue in verifier.get("issues", []))
        feedback.extend(str(issue) for issue in verifier.get("recommendations", []))
        missing_items = service.missing_items_for_markdown(draft, active_selected_items)
        missing_urls = [
            str(item.get("url", "")) for item in missing_items if item.get("url")
        ]
        if missing_items:
            draft = service.append_missing_items_section(draft, missing_items)
            draft = service.normalize_section_headings(
                service.dedupe_markdown_links(draft.strip())
            )
            draft = service.de_template_fields(draft)

        feedback_context = service.build_rewrite_feedback_context(
            gate=gate,
            critique=critique,
            verification=verifier,
            mode=mode,
            min_chars=int(evaluation["min_chars"]),
            target_chars=int(evaluation["target_chars"]),
            hard_max_chars=int(evaluation["hard_max_chars"]),
            rewrite_attempt=rewrites + 1,
            max_rewrites=max_rewrites,
            selected_items=active_selected_items,
            missing_selected_urls=missing_urls,
        )

        rewrites += 1
        rewritten_output = await service.writer_llm.revise_briefing(
            draft_markdown=draft,
            items=active_selected_items,
            feedback=feedback,
            feedback_context=feedback_context,
            recent_briefings=history,
            mode=mode,
            min_chars=int(evaluation["min_chars"]),
            target_chars=int(evaluation["target_chars"]),
            hard_max_chars=int(evaluation["hard_max_chars"]),
        )
        postprocessed = await service.postprocess_briefing(
            briefing_body=rewritten_output,
            selected_items=active_selected_items,
            mode=mode,
            min_chars=int(evaluation["min_chars"]),
            target_chars=int(evaluation["target_chars"]),
            hard_max_chars=int(evaluation["hard_max_chars"]),
        )
        rewritten_output = postprocessed.markdown
        active_selected_items = postprocessed.selected_items
        trace_rounds.append(
            {
                "round_index": len(trace_rounds),
                "phase": "initial" if not trace_rounds else "rewrite",
                "draft_input": round_input,
                "gate_result": dict(gate),
                "critique_result": dict(critique),
                "verifier_result": dict(verifier),
                "feedback": [str(item) for item in feedback],
                "rewrite_output": rewritten_output,
                "passed": False,
            }
        )
        preference_pairs.append(
            {
                "round_index": len(trace_rounds),
                "chosen_text": rewritten_output,
                "rejected_text": round_input,
                "rationale": service.build_preference_rationale(feedback),
                "source": "auto_writer_loop",
            }
        )
        draft = rewritten_output


async def simulate_briefing_body(
    service: Any,
    items: list[dict[str, Any]],
    recent_briefings: list[dict[str, Any]],
    *,
    apply_critic_rewrites: bool,
) -> tuple[str, dict[str, object]]:
    """Generate a replay candidate body without storing a draft row."""
    quiet_mode = service.is_quiet_day(items)
    mode = "quiet_day" if quiet_mode else "standard"
    min_chars, target_chars, hard_max_chars = service.char_limits(
        mode,
        selected_count=len(items),
    )
    active_items = list(items)

    briefing_body = await service.writer_llm.generate_briefing(
        active_items,
        recent_briefings=recent_briefings,
        mode=mode,
    )
    postprocessed = await service.postprocess_briefing(
        briefing_body=briefing_body,
        selected_items=active_items,
        mode=mode,
        min_chars=min_chars,
        target_chars=target_chars,
        hard_max_chars=hard_max_chars,
    )
    briefing_body = postprocessed.markdown
    active_items = postprocessed.selected_items
    min_chars, target_chars, hard_max_chars = service.char_limits(
        mode,
        selected_count=len(active_items),
    )

    rewrites = 0
    if apply_critic_rewrites and service.settings.briefing_critique_enabled:
        max_rewrites = max(0, int(service.settings.briefing_critique_max_rounds))
        while True:
            min_chars, target_chars, hard_max_chars = service.char_limits(
                mode,
                selected_count=len(active_items),
            )
            gate = service.quality_gate(
                markdown=briefing_body,
                selected_items=active_items,
                mode=mode,
                min_chars=min_chars,
                hard_max_chars=hard_max_chars,
            )
            critique = await service.critique_markdown(
                briefing_body,
                active_items,
                mode=mode,
                gate_hard_issues=[str(issue) for issue in gate.get("hard_issues", [])],
                gate_soft_issues=[str(issue) for issue in gate.get("soft_issues", [])],
                recent_briefings=recent_briefings,
            )
            gate_passed = bool(gate.get("passed", False))
            critic_passed = bool(critique.get("passed", False))
            if gate_passed and critic_passed:
                break
            if rewrites >= max_rewrites:
                break

            feedback: list[str] = []
            feedback.extend(gate.get("issues", []))
            feedback.extend([str(issue) for issue in critique.get("issues", [])])
            feedback.extend(
                [str(issue) for issue in critique.get("recommendations", [])]
            )
            missing_items = service.missing_items_for_markdown(briefing_body, active_items)
            feedback_context = service.build_rewrite_feedback_context(
                gate=gate,
                critique=critique,
                verification=service._default_verifier(),
                mode=mode,
                min_chars=min_chars,
                target_chars=target_chars,
                hard_max_chars=hard_max_chars,
                rewrite_attempt=rewrites + 1,
                max_rewrites=max_rewrites,
                selected_items=active_items,
                missing_selected_urls=[
                    str(item.get("url", "")) for item in missing_items if item.get("url")
                ],
            )

            rewrites += 1
            briefing_body = await service.writer_llm.revise_briefing(
                draft_markdown=briefing_body,
                items=active_items,
                feedback=feedback,
                feedback_context=feedback_context,
                mode=mode,
                min_chars=min_chars,
                target_chars=target_chars,
                hard_max_chars=hard_max_chars,
            )
            postprocessed = await service.postprocess_briefing(
                briefing_body=briefing_body,
                selected_items=active_items,
                mode=mode,
                min_chars=min_chars,
                target_chars=target_chars,
                hard_max_chars=hard_max_chars,
            )
            briefing_body = postprocessed.markdown
            active_items = postprocessed.selected_items

    briefing_body = service.normalize_section_headings(briefing_body)
    briefing_body = service.de_template_fields(briefing_body)
    min_chars, target_chars, hard_max_chars = service.char_limits(
        mode,
        selected_count=len(active_items),
    )

    meta = {
        "mode": mode,
        "rewrites": rewrites,
        "min_chars": min_chars,
        "hard_max_chars": hard_max_chars,
        "selected_items": list(active_items),
    }
    return briefing_body, meta


__all__ = [
    "generate_release_candidate",
    "simulate_briefing_body",
]
