## BCN Optimization Loop

This document defines the first phase of BCN's offline prompt/policy
optimization loop. The goal is to improve editorial quality safely using the
existing replay and benchmark lanes instead of jumping straight into model
training.

### Why This Exists

BCN already has:
- replay simulation against historical published briefings
- benchmark/shadow evaluation infrastructure
- stored generation traces, preference pairs, and outcome data

BCN does not yet have:
- enough human-labeled data for a strong production fine-tune
- a safe way to compare prompt/policy variants before promotion

The optimization loop fills that gap.

### Phase 1 Goals

Phase 1 is intentionally narrow:
- optimize prompts and a few release-threshold settings
- run only offline evaluations
- require explicit human promotion

Phase 1 does **not**:
- mutate arbitrary Python code
- self-deploy changes
- retrain models

### Variant Scope

Allowed in phase 1:
- writer prompt bundle overrides
- critic prompt override
- verifier prompt override
- settings overrides for a small set of knobs such as rewrite budget and
  critique thresholds

Not allowed in phase 1:
- collector logic
- persistence logic
- workflow orchestration
- distributor behavior

### Evaluation Stack

The optimization runner uses three evaluation sources:

1. Replay (`simulate`)
   - historical published briefings
   - regression guard against real prior output

2. Benchmark (`benchmark`)
   - curated or auto-built case pack
   - strongest quality gate for promotion decisions

3. Shadow (`shadow`)
   - advisory only for phase 1
   - not part of the promotion path yet

### Promotion Philosophy

Candidates are ranked, but not auto-promoted.

Hard rejection happens first. A candidate is rejected if it materially regresses:
- replay hard-pass rate
- replay human-writer pass rate
- replay formatting-clean pass rate
- duplicate-link issue rate
- benchmark case-pass rate

Surviving candidates get a composite score and a recommendation.

Human review is still required before the champion changes.

### Variant File Format

Variant files are JSON documents. Example:

```json
{
  "id": "writer-tighten-overclaim-v1",
  "description": "Reduce factual overclaim and boilerplate opener reuse",
  "settings_overrides": {
    "briefing_critique_max_rounds": 7,
    "writer_prompt_bundle_path": "optimization/prompts/writer-tighten-overclaim-v1.json",
    "verifier_prompt_path": "optimization/prompts/verifier-tighten-overclaim-v1.md"
  }
}
```

Relative prompt paths are resolved relative to the variant file.

### First Implementation

The first runnable implementation provides:
- a persisted `optimize-run`
- champion replay run
- candidate replay run
- benchmark run
- a candidate summary with hard-reject decisions and composite score

### Immediate Next Steps After Phase 1

1. Review and tighten the first curated benchmark pack
   (`benchmark_packs/core_v1.json`).
2. Start recording lightweight human editorial review labels on accepted/edited
   briefings.
3. Add dashboard visibility for optimization runs.
4. Add a constrained proposer for prompt variants after the scoring loop is
   trusted.
